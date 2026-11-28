"""
Huấn luyện mô hình demo: logistic regression 3 nhãn trên đặc trưng cửa sổ 120 s (aiot/source_window.py).

1. Sinh phiên 1 giây có nhãn bằng training/demo_data.py (hiệu chỉnh theo số liệu thật, seed cố định).
2. Tính đặc trưng mỗi 5 giây, đúng code chạy trên Pi.
3. Chia theo PHIÊN 70/15/15 trong từng kiểu phiên.
4. StandardScaler + LogisticRegression(class_weight="balanced"); chọn C theo ĐIỂM DEMO trên tập val.
5. Chấm điểm demo trên tập test: phát lại qua bước làm mượt giống controller (TB xác suất 3 lần đoán,
   chỉ đổi nhãn khi >= 0,6) rồi đo: % bình thường khi để yên, báo nhầm/giờ, độ trễ phát hiện,
   thời gian về bình thường, xác suất của nhãn đúng.
6. Chấm trên số liệu THẬT (fast_readings.csv 1 s, raw_readings.csv 10 s) với nhãn tham chiếu tính
   bằng đúng luật "thầy" của demo_data trên tín hiệu thật đã lọc trung vị 5 s.
7. Xuất models/source_logreg.json (suy luận numpy trên Pi) và models/source_logreg_summary.json.

Chạy: .venv/Scripts/python -m training.train_logreg [--per-kind 60] [--seed 42] [--no-cache]
"""
import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.preprocessing import StandardScaler

from aiot.source_model import LABELS, LogRegModel, spec_from_sklearn
from aiot.source_window import (BASE_S, COLUMNS, PM_OFFSET, STEP_S, WINDOW_FEATURES,
                                compute_window_features)
from training.demo_data import (COOK_RATIO, DUST_RATIO, HUM_RISE, KINDS, NORMAL_ONLY, TEMP_RISE,
                                PM_SENSOR_MAX, fit_real, save_real, session)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "training" / "data"
MODELS = ROOT / "models"
VOTE_N, MIN_CONF = 3, 0.6
N, C, P = (LABELS.index(x) for x in ("normal", "cooking", "pollution"))
LIMIT_S = {C: 30, P: 15}                # trễ phát hiện tối đa cho demo
RECOVER_S = 60
C_GRID = (0.01, 0.1, 1.0, 10.0)
EXTRA = {"idle": 2, "back_to_back": 2, "hum_only": 2, "small_bump": 2}  # số lần nhân thêm phiên của kiểu này

CRITERIA = {
    "normal_frac": 0.99, "false_alarm_per_h": 0.2, "median_p_normal": 0.9,
    "cooking_detect": 0.95, "pollution_detect": 0.95, "median_p_event": 0.8,
    "recover_rate": 0.95, "real_normal_frac": 0.98,
}


# ---------------- đặc trưng ----------------
def windows(arr, start=BASE_S, step=STEP_S):
    """(X, y, t): mỗi step giây từ giây start, bộ đệm BASE_S giây kết thúc ở giây đó (đệm NaN
    phía trước như controller khi lịch sử còn ngắn)."""
    n = len(arr["pm25"])
    pad = {c: np.concatenate([np.full(BASE_S, np.nan), np.asarray(arr[c], float)]) for c in COLUMNS}
    X, y, t = [], [], []
    for i in range(start, n, step):
        j = i + BASE_S + 1
        f = compute_window_features({c: pad[c][j - BASE_S:j] for c in COLUMNS})
        if f is None:
            continue
        X.append(f)
        y.append(int(arr["label"][i]))
        t.append(i)
    return np.array(X).reshape(-1, len(WINDOW_FEATURES)), np.array(y, int), np.array(t, int)


def build(real, per_kind, seed):
    rng = np.random.default_rng(seed)
    Xs, ys, gs, ts, kinds = [], [], [], [], []
    k = 0
    for kind in KINDS:
        for _ in range(per_kind * EXTRA.get(kind, 1)):
            X, y, t = windows(session(rng, real, kind))
            Xs.append(X), ys.append(y), ts.append(t), gs.append(np.full(len(y), k))
            kinds.append(kind)
            k += 1
    return np.vstack(Xs), np.concatenate(ys), np.concatenate(gs), np.concatenate(ts), np.array(kinds)


def split(kinds, seed):
    """Chỉ số phiên train/val/test, chia 70/15/15 trong từng kiểu phiên."""
    rng = np.random.default_rng(seed + 1)
    tr, va, te = [], [], []
    for kind in KINDS:
        ids = rng.permutation(np.where(kinds == kind)[0])
        a, b = int(0.7 * len(ids)), int(0.85 * len(ids))
        tr += ids[:a].tolist()
        va += ids[a:b].tolist()
        te += ids[b:].tolist()
    return np.array(tr), np.array(va), np.array(te)


# ---------------- làm mượt như controller (aiot/source_model.smooth, chạy nhanh trên mảng) ----------------
def smoothed(proba):
    """Nhãn sau làm mượt cho chuỗi xác suất liên tiếp 5 s (theo LABELS); -1 = chưa có nhãn."""
    out, label, hist = [], -1, []
    for p in proba:
        hist = (hist + [p])[-VOTE_N:]
        mean = np.mean(hist, axis=0)
        cand = int(np.argmax(mean))
        if cand != label and mean[cand] >= MIN_CONF:
            label = cand
        out.append(label)
    return np.array(out)


def _segments(y):
    """[(nhãn, đầu, cuối)] các đoạn liên tiếp cùng nhãn (chỉ số cửa sổ, cuối tính cả)."""
    segs, s = [], 0
    for i in range(1, len(y) + 1):
        if i == len(y) or y[i] != y[s]:
            segs.append((int(y[s]), s, i - 1))
            s = i
    return segs


# ---------------- điểm demo ----------------
def demo_score(model, X, y, g, kinds, ids):
    stat = {"normal_windows": 0, "normal_ok": 0, "normal_hours": 0.0, "false_alarms": 0,
            "p_normal": [], "p_event": [], "events": {C: [0, 0], P: [0, 0]},
            "latency": {C: [], P: []}, "wrong_flash": 0, "recover": [0, 0], "recover_s": []}
    per_kind = {}
    for k in ids:
        m = g == k
        if not m.any():
            continue
        proba = model.predict_proba_batch(X[m])
        sm, yt = smoothed(proba), y[m]
        kind = kinds[k]
        pk = per_kind.setdefault(kind, {"windows": 0, "agree": 0})
        pk["windows"] += len(yt)
        pk["agree"] += int(np.sum(sm == yt))
        if kind in NORMAL_ONLY:
            stat["normal_windows"] += len(sm)
            stat["normal_ok"] += int(np.sum(sm == N))
            stat["normal_hours"] += len(sm) * STEP_S / 3600
            stat["false_alarms"] += int(np.sum((sm[1:] != sm[:-1]) & (sm[1:] != N) & (sm[1:] >= 0)))
            stat["p_normal"] += proba[:, N].tolist()
        for lab, a, b in _segments(yt):
            if lab == N or b - a < 2:
                continue
            stat["events"][lab][1] += 1
            stat["p_event"] += proba[a:b + 1, lab].tolist()
            hit = np.where(sm[a:b + 1] == lab)[0]
            lim = LIMIT_S[lab] // STEP_S
            if hit.size and hit[0] <= lim:
                stat["events"][lab][0] += 1
            stat["latency"][lab].append(int(hit[0]) * STEP_S if hit.size else None)
            other = C if lab == P else P
            stat["wrong_flash"] += int(np.sum(sm[a:b + 1] == other))
            nxt = next((s for s in _segments(yt) if s[1] == b + 1), None)
            # chỉ tính "về bình thường" khi đoạn bình thường sau đó đủ dài (đợt bụi lặp lại
            # sau 20–60 s là cùng một sự kiện)
            if nxt is not None and nxt[0] == N and (nxt[2] - nxt[1] + 1) * STEP_S >= RECOVER_S + 30:
                stat["recover"][1] += 1
                back = np.where(sm[b + 1:] == N)[0]
                if back.size and back[0] <= RECOVER_S // STEP_S:
                    stat["recover"][0] += 1
                stat["recover_s"].append(int(back[0]) * STEP_S if back.size else None)

    def med(v):
        v = [x for x in v if x is not None]
        return float(np.median(v)) if v else None

    res = {
        "normal_frac": stat["normal_ok"] / max(stat["normal_windows"], 1),
        "false_alarm_per_h": stat["false_alarms"] / max(stat["normal_hours"], 1e-9),
        "median_p_normal": med(stat["p_normal"]),
        "cooking_detect": stat["events"][C][0] / max(stat["events"][C][1], 1),
        "pollution_detect": stat["events"][P][0] / max(stat["events"][P][1], 1),
        "median_p_event": med(stat["p_event"]),
        "recover_rate": stat["recover"][0] / max(stat["recover"][1], 1),
        "cooking_latency_median_s": med(stat["latency"][C]),
        "pollution_latency_median_s": med(stat["latency"][P]),
        "recover_median_s": med(stat["recover_s"]),
        "wrong_label_windows": stat["wrong_flash"],
        "events": {LABELS[c]: stat["events"][c][1] for c in (C, P)},
        "agree_by_kind": {k: round(v["agree"] / v["windows"], 3) for k, v in per_kind.items()},
    }
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in res.items()}


def selection_score(s):
    return (s["normal_frac"] + s["cooking_detect"] + s["pollution_detect"] + s["recover_rate"]
            + (s["median_p_event"] or 0) - 0.1 * s["false_alarm_per_h"] - 0.001 * s["wrong_label_windows"])


# ---------------- số liệu thật ----------------
def real_arrays():
    """[(tên, dict mảng 1 giây + 'label' tham chiếu)] từ số liệu thật; khoảng mất mẫu > 60 s
    tách thành đoạn riêng."""
    out = []
    for name, file, hold in (("fast_1s", "fast_readings.csv", 1), ("raw_10s_23_11", "raw_readings.csv", 10)):
        path = DATA / file
        if not path.exists():
            continue
        df = pd.read_csv(path).sort_values("ts").drop_duplicates("ts")
        if name.startswith("raw"):
            df = df[df.ts < 1795449600]                 # chỉ tối 23/11 (sau đó đã có fast_1s)
        gaps = np.where(np.diff(df.ts.to_numpy()) > 60)[0]
        bounds = np.split(np.arange(len(df)), gaps + 1)
        for part in bounds:
            seg = df.iloc[part]
            if len(seg) * hold < BASE_S // 6:
                continue
            t0, t1 = int(seg.ts.min()), int(seg.ts.max())
            s = seg.set_index("ts")[list(COLUMNS)].reindex(range(t0, t1 + 1))
            s = s.ffill(limit=hold - 1) if hold > 1 else s
            arr = {c: s[c].to_numpy(float) for c in COLUMNS}
            for c in ("pm1", "pm25", "pm10"):           # như aiot.db.fast_rows: ghim số đọc bão hoà
                arr[c] = np.where(arr[c] > PM_SENSOR_MAX, PM_SENSOR_MAX, arr[c])
            arr["label"] = reference_labels(s)
            arr["_t0"] = t0
            out.append((f"{name}@{pd.Timestamp(t0, unit='s', tz='Asia/Ho_Chi_Minh'):%d/%m %H:%M}", arr))
    return out


def reference_labels(s):
    """Luật "thầy" của demo_data áp lên tín hiệu thật đã lọc trung vị 5 s."""
    f = s.rolling(5, min_periods=1, center=True).median()
    p10 = lambda x: x.rolling(BASE_S, min_periods=1).quantile(0.1)
    ratio = (f.pm25.clip(upper=PM_SENSOR_MAX) + PM_OFFSET) / (p10(f.pm25) + PM_OFFSET)
    warm = (f.hum - p10(f.hum) >= HUM_RISE) | (f.temp - p10(f.temp) >= TEMP_RISE)
    lab = np.full(len(s), N)
    lab[((ratio >= DUST_RATIO) & ~warm).to_numpy()] = P
    lab[((ratio >= COOK_RATIO) & warm).to_numpy()] = C
    return lab


def evaluate_real(model):
    res = {}
    for name, arr in real_arrays():
        X, y, t = windows(arr, start=60)
        if not len(y):
            continue
        proba = model.predict_proba_batch(X)
        sm = smoothed(proba)
        per = {}
        for c in (N, C, P):
            m = y == c
            if m.any():
                per[LABELS[c]] = {"windows": int(m.sum()), "agree": round(float(np.mean(sm[m] == c)), 3),
                                  "predicted": {LABELS[k] if k >= 0 else "none": int(np.sum(sm[m] == k))
                                                for k in np.unique(sm[m])}}
        events = []
        for lab, a, b in _segments(y):
            if lab != N:
                hit = np.where(sm[a:b + 1] == lab)[0]
                ts = int(arr["_t0"] + t[a])
                events.append({"at": f"{pd.Timestamp(ts, unit='s', tz='Asia/Ho_Chi_Minh'):%d/%m %H:%M:%S}",
                               "label": LABELS[lab], "duration_s": int((b - a + 1) * STEP_S),
                               "latency_s": int(hit[0]) * STEP_S if hit.size else None,
                               "max_p": round(float(proba[a:b + 1, lab].max()), 3)})
        res[name] = {"windows": int(len(y)), "per_label": per, "events": events,
                     "median_p_normal_when_normal": round(float(np.median(proba[y == N, N])), 3)
                     if (y == N).any() else None}
    return res


# ---------------- main ----------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Huấn luyện mô hình demo logistic regression")
    ap.add_argument("--per-kind", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-cache", action="store_true", help="sinh lại dữ liệu (sau khi sửa demo_data)")
    a = ap.parse_args(argv)
    t_start = time.time()

    real = fit_real()
    save_real(real)
    cache = DATA / f"_logreg_features_{a.per_kind}_{a.seed}.npz"
    if cache.exists() and not a.no_cache:
        d = np.load(cache)
        X, y, g, t, kinds = d["X"], d["y"], d["g"], d["t"], d["kinds"]
    else:
        X, y, g, t, kinds = build(real, a.per_kind, a.seed)
        np.savez_compressed(cache, X=X, y=y, g=g, t=t, kinds=kinds)
    print(f"{len(kinds)} phiên, {len(y)} cửa sổ, {X.shape[1]} đặc trưng, "
          f"nhãn {dict(zip(LABELS, np.bincount(y, minlength=3).tolist()))}, {time.time() - t_start:.0f} s")

    tr_ids, va_ids, te_ids = split(kinds, a.seed)
    tr = np.isin(g, tr_ids)
    scaler = StandardScaler().fit(X[tr])
    Ztr = scaler.transform(X[tr])

    best, grid = None, {}
    for c in C_GRID:
        lr = LogisticRegression(C=c, class_weight="balanced", max_iter=3000).fit(Ztr, y[tr])
        m = LogRegModel(spec_from_sklearn(scaler, lr, [LABELS[k] for k in lr.classes_]))
        s = demo_score(m, X, y, g, kinds, va_ids)
        grid[c] = {"score": round(selection_score(s), 4), **{k: s[k] for k in CRITERIA if k in s}}
        print(f"C={c:<5} điểm val {selection_score(s):.4f}  {grid[c]}")
        if best is None or selection_score(s) > best[0]:
            best = (selection_score(s), c, lr)
    _, c_best, lr = best

    meta = {"trained": time.strftime("%Y-%m-%d %H:%M"), "C": c_best, "per_kind": a.per_kind,
            "seed": a.seed, "seq_s": 120, "base_s": BASE_S, "real_fit": {
                k: v for k, v in real.items() if k != "dust"},
            "real_dust_templates": [{"peak": round(t_["peak"], 2), "f1": round(t_["f1"], 3),
                                     "f10": round(t_["f10"], 3)} for t_ in real["dust"]]}
    spec = spec_from_sklearn(scaler, lr, [LABELS[k] for k in lr.classes_], meta=meta)
    MODELS.mkdir(exist_ok=True)
    (MODELS / "source_logreg.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    model = LogRegModel(spec)

    te = np.isin(g, te_ids)
    sk = lr.predict_proba(scaler.transform(X[te][:2000]))
    assert np.allclose(model.predict_proba_batch(X[te][:2000]), sk, atol=1e-6), "numpy khác sklearn"

    pred = np.argmax(model.predict_proba_batch(X[te]), axis=1)
    test = demo_score(model, X, y, g, kinds, te_ids)
    real_res = evaluate_real(model)
    real_normal = [v["per_label"]["normal"]["agree"] for v in real_res.values() if "normal" in v["per_label"]]
    test["real_normal_frac"] = round(float(np.mean(real_normal)), 4) if real_normal else None

    checks = {}
    for k, thr in CRITERIA.items():
        v = test.get(k)
        checks[k] = {"value": v, "target": thr,
                     "pass": v is not None and (v <= thr if k == "false_alarm_per_h" else v >= thr)}
    names = list(WINDOW_FEATURES)
    top = {}
    for i, c in enumerate(lr.classes_):
        order = np.argsort(-np.abs(lr.coef_[i]))[:8]
        top[LABELS[c]] = [(names[j], round(float(lr.coef_[i][j]), 3)) for j in order]

    summary = {
        "labels": LABELS, "features": names, "meta": meta, "grid": grid,
        "sessions": {"train": len(tr_ids), "val": len(va_ids), "test": len(te_ids)},
        "windows": int(len(y)),
        "test_report": classification_report(y[te], pred, target_names=list(LABELS), output_dict=True,
                                              zero_division=0),
        "test_confusion": confusion_matrix(y[te], pred, labels=[0, 1, 2]).tolist(),
        "demo_test": test, "criteria": checks, "real": real_res, "top_coef": top,
    }
    (MODELS / "source_logreg_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"\nC chọn: {c_best}")
    print(classification_report(y[te], pred, target_names=list(LABELS), digits=3, zero_division=0))
    print("Ma trận nhầm lẫn (hàng = thật):", summary["test_confusion"])
    print("\nTiêu chí demo (test sinh ra):")
    for k, v in checks.items():
        print(f"  {'ĐẠT ' if v['pass'] else 'CHƯA'}  {k:<20} {v['value']}  (mục tiêu {v['target']})")
    print("  độ trễ trung vị: nấu ăn", test["cooking_latency_median_s"], "s, bụi mịn",
          test["pollution_latency_median_s"], "s; về bình thường", test["recover_median_s"], "s")
    print("\nSố liệu thật:")
    for name, v in real_res.items():
        print(f"  {name}: {v['windows']} cửa sổ, {json.dumps(v['per_label'], ensure_ascii=False)}")
        for e in v["events"]:
            print(f"     sự kiện {e}")
    print(f"\nXong trong {time.time() - t_start:.0f} s -> {MODELS / 'source_logreg.json'}")
    return 0 if all(v["pass"] for v in checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
