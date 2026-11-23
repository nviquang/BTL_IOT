"""
Huấn luyện bộ nhận diện nguồn bụi (normal / cooking / pollution / smoking).

1. Sinh dữ liệu mô phỏng (training/simulate_sources.py, seed cố định).
2. Tính đặc trưng mỗi 5 giây (aiot/source_features.py, đúng code chạy trên Pi).
3. Chia theo PHIÊN 70/15/15 (không trộn cửa sổ của cùng phiên giữa train và test).
4. So sánh: luật ngưỡng viết tay, cây quyết định nông, RandomForest.
5. Đánh giá trên tập test mô phỏng (F1 từng lớp, ma trận nhầm lẫn, độ trễ phát hiện sau
   làm mượt, số lần báo nhầm mỗi giờ) và trên dữ liệu THẬT có nhãn (training/data/
   raw_readings.csv 10 giây + real_labels.csv, cùng fast_readings.csv 1 giây khi có).
6. Xuất models/source_rf.json (suy luận bằng numpy trên Pi) + models/source_summary.json.

Chạy: .venv/Scripts/python -m training.train_source [--sessions 400] [--seed 42]
"""
import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix, f1_score
from sklearn.tree import DecisionTreeClassifier

from aiot.source_features import COLUMNS, FEATURES, STEP_S, WINDOW_S, compute_features
from aiot.source_model import FORMAT, LABELS, SourceModel, SourceState, smooth
from training.simulate_sources import WARMUP_S, simulate

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "training" / "data"
MODELS = ROOT / "models"
VOTE_N, MIN_CONF = 3, 0.6


# ---------------- đặc trưng ----------------
def session_windows(arr, step=STEP_S, start=WARMUP_S):
    """(X, y, t) của một phiên: mỗi step giây từ start, bộ đệm WINDOW_S giây (đệm NaN)."""
    n = arr["pm25"].size
    pad = {c: np.concatenate([np.full(WINDOW_S, np.nan), np.asarray(arr[c], float)]) for c in COLUMNS}
    X, y, t = [], [], []
    for i in range(start, n, step):
        j = i + WINDOW_S + 1
        f = compute_features({c: pad[c][j - WINDOW_S:j] for c in COLUMNS})
        if f is None:
            continue
        X.append(f)
        y.append(int(arr["label"][i]) if "label" in arr else -1)
        t.append(i)
    return np.array(X), np.array(y), np.array(t)


def build(sessions):
    Xs, ys, gs, ts = [], [], [], []
    for k, (arr, _) in enumerate(sessions):
        X, y, t = session_windows(arr)
        Xs.append(X), ys.append(y), gs.append(np.full(len(y), k)), ts.append(t)
    return np.vstack(Xs), np.concatenate(ys), np.concatenate(gs), np.concatenate(ts)


# ---------------- đối chứng: luật viết tay ----------------
def rule_predict(X):
    f = {k: X[:, i] for i, k in enumerate(FEATURES)}
    out = np.full(len(X), LABELS.index("normal"))
    pol = f["log_base"] >= np.log1p(55.5)
    out[pol] = LABELS.index("pollution")
    cook = (f["exc_rel60"] > 0.3) & ((f["dh_base"] > 2) | (f["dt_base"] > 0.3))
    out[cook] = LABELS.index("cooking")
    smoke = (f["exc_rel5"] > 0.8) & ((f["cv60"] > 0.15) | (f["jumps300"] >= 2)) & (f["dh300"] < 2)
    out[smoke] = LABELS.index("smoking")
    return out


# ---------------- xuất JSON ----------------
def export_forest(rf, path, meta):
    trees = []
    for est in rf.estimators_:
        tr = est.tree_
        value = tr.value[:, 0, :]
        trees.append({
            "left": tr.children_left.tolist(), "right": tr.children_right.tolist(),
            "feature": [int(v) if v >= 0 else 0 for v in tr.feature],
            "threshold": [float(v) for v in tr.threshold],
            "value": np.round(value / np.maximum(value.sum(1, keepdims=True), 1e-12), 6).tolist(),
        })
    spec = {"format": FORMAT, "name": "source_rf", "classes": [LABELS[c] for c in rf.classes_],
            "features": list(FEATURES), "trees": trees, "meta": meta}
    path.write_text(json.dumps(spec, separators=(",", ":")), encoding="utf-8")
    return SourceModel(spec)


# ---------------- đánh giá theo thời gian (sau làm mượt) ----------------
def smoothed_labels(proba):
    st, out = SourceState(), []
    for k, p in enumerate(proba):
        st = smooth(st, p, k * STEP_S, vote_n=VOTE_N, min_confidence=MIN_CONF)
        out.append(LABELS.index(st.label) if st.label else -1)
    return np.array(out)


def temporal_metrics(sessions, test_ids, X, y, g, t, model):
    """Độ trễ phát hiện mỗi sự kiện và số lần báo nhầm mỗi giờ khi nhãn thật là normal."""
    latency = {"cooking": [], "smoking": []}
    missed = {"cooking": 0, "smoking": 0}
    false_alarms, normal_s = 0, 0
    accs = []
    for k in test_ids:
        m = g == k
        if not m.any():
            continue
        sm = smoothed_labels(model.predict_proba_batch(X[m]))
        yt, tt = y[m], t[m]
        accs.append(np.mean(sm == yt))
        for ev in sessions[k][1]["events"]:
            s0, s1 = ev["start"], ev["end"]
            idx = np.where((tt >= s0) & (tt <= s1) & (sm == LABELS.index(ev["label"])))[0]
            if idx.size:
                latency[ev["label"]].append(int(tt[idx[0]] - s0))
            else:
                missed[ev["label"]] += 1
        nm = yt == LABELS.index("normal")
        normal_s += nm.sum() * STEP_S
        prev = LABELS.index("normal")
        for lab, is_n in zip(sm, nm):
            if is_n and lab not in (-1, LABELS.index("normal")) and lab != prev:
                false_alarms += 1
            prev = lab
    return {
        "latency_median_s": {k: (float(np.median(v)) if v else None) for k, v in latency.items()},
        "latency_p90_s": {k: (float(np.percentile(v, 90)) if v else None) for k, v in latency.items()},
        "events_missed": missed,
        "events_detected": {k: len(v) for k, v in latency.items()},
        "false_alarms_per_hour_normal": round(false_alarms / max(normal_s / 3600, 1e-9), 3),
        "smoothed_accuracy": round(float(np.mean(accs)), 4),
    }


# ---------------- dữ liệu thật ----------------
def real_sets():
    """[(tên, dict mảng 1 giây có 'label' (-1 = chưa gán nhãn))] từ dữ liệu Pi."""
    labels_path = DATA / "real_labels.csv"
    if not labels_path.exists():
        return []
    labels = pd.read_csv(labels_path)
    out = []
    for name, file, hold in (("raw_10s", "raw_readings.csv", 10), ("fast_1s", "fast_readings.csv", 1)):
        path = DATA / file
        if not path.exists():
            continue
        df = pd.read_csv(path).sort_values("ts")
        if df.empty:
            continue
        t0, t1 = int(df.ts.min()), int(df.ts.max())
        idx = pd.RangeIndex(t0, t1 + 1)
        # Mẫu 10 giây được giữ nguyên trong 10 giây (bước nhảy) để thành chuỗi 1 giây
        s = df.set_index("ts")[list(COLUMNS)].reindex(idx).ffill(limit=hold - 1)
        arr = {c: s[c].to_numpy(float) for c in COLUMNS}
        lab = np.full(len(idx), -1, dtype=int)
        for r in labels.itertuples():
            a, b = max(r.start_ts, t0) - t0, min(r.end_ts, t1) - t0
            if b >= a:
                lab[a:b + 1] = LABELS.index(r.label)
        arr["label"] = lab
        arr["_t0"] = t0
        out.append((name, arr))
    return out


def evaluate_real(model, rule=False):
    res = {}
    for name, arr in real_sets():
        X, y, t = session_windows(arr, start=60)
        if not len(y):
            continue
        keep = y >= 0
        if rule:
            pred = rule_predict(X)
        else:
            pred = smoothed_labels(model.predict_proba_batch(X))
        per = {}
        for c, lab in enumerate(LABELS):
            m = keep & (y == c)
            if m.any():
                per[lab] = {"windows": int(m.sum()), "accuracy": round(float(np.mean(pred[m] == c)), 3),
                            "predicted": {LABELS[k]: int(np.sum(pred[m] == k)) for k in np.unique(pred[m]) if k >= 0}}
        res[name] = per
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Huấn luyện bộ nhận diện nguồn bụi")
    ap.add_argument("--sessions", type=int, default=400)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-cache", action="store_true", help="tính lại đặc trưng (sau khi sửa mô phỏng)")
    a = ap.parse_args(argv)
    t_start = time.time()

    sessions = simulate(a.sessions, a.seed)
    cache = DATA / f"_source_features_{a.sessions}_{a.seed}.npz"
    if cache.exists() and not a.no_cache:
        d = np.load(cache)
        X, y, g, t = d["X"], d["y"], d["g"], d["t"]
    else:
        X, y, g, t = build(sessions)
        np.savez_compressed(cache, X=X, y=y, g=g, t=t)
    print(f"{len(y)} cửa sổ, {X.shape[1]} đặc trưng, tính trong {time.time() - t_start:.0f} s")

    rng = np.random.default_rng(a.seed)
    ids = rng.permutation(a.sessions)
    n_tr, n_va = int(0.7 * a.sessions), int(0.15 * a.sessions)
    tr_ids, va_ids, te_ids = ids[:n_tr], ids[n_tr:n_tr + n_va], ids[n_tr + n_va:]
    tr, va, te = np.isin(g, tr_ids), np.isin(g, va_ids), np.isin(g, te_ids)

    results = {}
    pred_rule = rule_predict(X[te])
    results["rule"] = {"macro_f1": round(f1_score(y[te], pred_rule, average="macro"), 4)}
    dt = DecisionTreeClassifier(max_depth=4, class_weight="balanced", random_state=a.seed).fit(X[tr], y[tr])
    results["tree_d4"] = {"macro_f1": round(f1_score(y[te], dt.predict(X[te]), average="macro"), 4)}

    best = None
    for depth in (8, 12):
        rf = RandomForestClassifier(n_estimators=100, max_depth=depth, min_samples_leaf=20,
                                    class_weight="balanced", n_jobs=-1, random_state=a.seed)
        rf.fit(X[tr], y[tr])
        f1v = f1_score(y[va], rf.predict(X[va]), average="macro")
        print(f"RF max_depth={depth}: macro F1 (val) = {f1v:.4f}")
        if best is None or f1v > best[0] + 0.002:
            best = (f1v, depth, rf)
    _, depth, rf = best
    pred = rf.predict(X[te])
    results["random_forest"] = {"max_depth": depth, "macro_f1": round(f1_score(y[te], pred, average="macro"), 4)}

    MODELS.mkdir(exist_ok=True)
    meta = {"trained_at": int(time.time()), "seed": a.seed, "sessions": a.sessions,
            "vote_n": VOTE_N, "min_confidence": MIN_CONF}
    model = export_forest(rf, MODELS / "source_rf.json", meta)
    sk = rf.predict_proba(X[te][:2000])
    js = model.predict_proba_batch(X[te][:2000])
    assert np.allclose(js[:20], [model.predict_proba(x) for x in X[te][:20]])
    assert np.max(np.abs(sk - js)) < 1e-4, "JSON khác sklearn"

    report = classification_report(y[te], pred, target_names=LABELS, digits=3, output_dict=True)
    cm = confusion_matrix(y[te], pred).tolist()
    temporal = temporal_metrics(sessions, te_ids, X, y, g, t, model)
    real_rf, real_rule = evaluate_real(model), evaluate_real(model, rule=True)
    importance = sorted(zip(FEATURES, rf.feature_importances_), key=lambda kv: -kv[1])

    summary = {
        "labels": list(LABELS), "features": list(FEATURES), "meta": meta,
        "split_sessions": {"train": len(tr_ids), "val": len(va_ids), "test": len(te_ids)},
        "windows": {"train": int(tr.sum()), "val": int(va.sum()), "test": int(te.sum())},
        "test_sim": results, "rf_report": report, "rf_confusion": cm, "rf_temporal": temporal,
        "real_rf_smoothed": real_rf, "real_rule": real_rule,
        "feature_importance": [[k, round(float(v), 4)] for k, v in importance],
    }
    (MODELS / "source_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\nTest mô phỏng (macro F1):", {k: v["macro_f1"] for k, v in results.items()})
    print(classification_report(y[te], pred, target_names=LABELS, digits=3))
    print("Ma trận nhầm lẫn (hàng = thật):", LABELS)
    for lab, row in zip(LABELS, cm):
        print(f"  {lab:9s} {row}")
    print("Theo thời gian (sau làm mượt):", json.dumps(temporal, ensure_ascii=False))
    print("Dữ liệu thật RF:", json.dumps(real_rf, ensure_ascii=False))
    print("Dữ liệu thật luật:", json.dumps(real_rule, ensure_ascii=False))
    print("Đặc trưng quan trọng:", importance[:8])
    print(f"Xong trong {time.time() - t_start:.0f} s -> {MODELS / 'source_rf.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
