"""
Huấn luyện mô hình demo nhận diện nguồn bụi theo sự kiện (docs/de_xuat_ai_demo_v3.md).

Chỉ dùng dữ liệu THẬT thu trong phòng demo: fast_readings (mẫu 1 giây) + source_labels
(scripts/label_event.py), kéo về bằng training/export_fast.py.

1. Phát lại mẫu 1 giây qua đúng máy trạng thái chạy trên Pi (aiot/source_event.py), tách
   các sự kiện. Mỗi sự kiện giữ ĐƯỜNG CONG nhiều kênh của cả cửa sổ (shape) và vector
   8 số tóm tắt ở mỗi giây từ classify_from_s tới lock_after_s.
2. Gán nhãn sự kiện theo khoảng nhãn chồng lên nhiều nhất. Sự kiện trong khoảng 'normal'
   (đi lại, thở gần, bật đèn...) thành mẫu 'normal'. Sự kiện không có nhãn bị bỏ, có báo.
3. Đánh giá leave-one-event-out (mỗi lần làm đạo cụ là một nhóm): độ đúng lúc khoá nhãn,
   độ đúng ở lần đoán đầu (giây 8), độ trễ tới nhãn đúng, ma trận nhầm lẫn. Mô hình chính
   là kNN-DTW trên đường cong; so với kNN trên 8 số tóm tắt, luật viết tay, cây quyết định
   độ sâu 3 và RandomForest mô phỏng cũ (nếu có).
4. Xuất models/source_shape.json (aiot-shape-knn-v1, mô hình chính), models/source_knn.json
   (aiot-knn-v1, dùng để giải thích), models/source_knn_summary.json và bản đồ sự kiện
   models/source_knn_map.png (d_hum - ex_r10).

Chạy: .venv/Scripts/python -m training.train_demo [--db training/data/pi_aiot.db]
"""
import argparse
import json
import sqlite3
import time
from pathlib import Path

import numpy as np
import pandas as pd

from aiot.source_event import EVENT_FEATURES, EventCfg, TrackerState, features, shape, update
from aiot.source_model import LABELS, KnnModel, ShapeKnnModel, save_model

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "training" / "data"
MODELS = ROOT / "models"
FAST_COLS = ("pm1", "pm25", "pm10", "n03", "n05", "n10", "n25", "n50", "n100",
             "temp", "hum", "lux")
FI = {f: i for i, f in enumerate(EVENT_FEATURES)}


# ---------------- dữ liệu ----------------
def load_data(db: Path | None = None, fast_csv: Path | None = None, labels_csv: Path | None = None):
    """(rows [(ts, {kênh: giá trị})], DataFrame nhãn start_ts,end_ts,label)."""
    if db is not None:
        conn = sqlite3.connect(db)
        fast = pd.read_sql_query("SELECT * FROM fast_readings ORDER BY ts", conn)
        labels = pd.read_sql_query("SELECT start_ts, end_ts, label FROM source_labels", conn)
        conn.close()
    else:
        fast = pd.read_csv(fast_csv or DATA / "fast_readings.csv").sort_values("ts")
        if labels_csv is None:
            labels_csv = DATA / "source_labels.csv"
            if not labels_csv.exists():
                labels_csv = DATA / "real_labels.csv"
        labels = pd.read_csv(labels_csv)[["start_ts", "end_ts", "label"]]
    cols = [c for c in FAST_COLS if c in fast.columns]
    rows = []
    for r in fast[["ts", *cols]].itertuples(index=False):
        vals = {c: (None if pd.isna(v) else float(v)) for c, v in zip(cols, r[1:])}
        rows.append((int(r[0]), vals))
    return rows, labels


def extract_events(rows, cfg: EventCfg = EventCfg()):
    """[{start, end, snaps: {tuổi: vector}, shape: mảng, shape_len: {tuổi: số hàng}}] của mọi
    sự kiện máy trạng thái phát hiện (bỏ sự kiện ngắn hơn classify_from_s)."""
    st = TrackerState()
    events = []
    cur = None
    for ts, vals in rows:
        prev_n = st.n_events
        st = update(st, ts, vals, None, cfg)
        if st.n_events != prev_n:
            cur = {"start": st.start_ts, "end": ts, "snaps": {}, "shape": None, "shape_len": {}}
            events.append(cur)
        if cur is not None and st.phase in ("event", "cooldown") and st.n_events == len(events):
            cur["end"] = ts
            age = st.age_s
            if st.phase == "event" and cfg.classify_from_s <= age <= cfg.lock_after_s:
                cur["snaps"][age] = features(st, cfg)
                cur["shape"] = shape(st)
                cur["shape_len"][age] = len(cur["shape"])
    # sự kiện kết thúc trước giây khoá: lấy vector cuối làm vector khoá
    return [e for e in events if e["snaps"]]


def label_events(events, labels: pd.DataFrame):
    """Gán e['label'] theo khoảng nhãn chồng lên nhiều nhất (None nếu không chồng)."""
    for e in events:
        best, best_ov = None, 0
        for r in labels.itertuples(index=False):
            ov = min(e["end"], int(r.end_ts)) - max(e["start"], int(r.start_ts))
            if ov >= 0 and ov + 1 > best_ov:
                best, best_ov = r.label, ov + 1
        e["label"] = best
    return events


def lock_vec(e):
    return e["snaps"][max(e["snaps"])]


# ---------------- mô hình và đối chứng ----------------
def rule_label(x):
    """Luật viết tay trên vài đặc trưng, để so với kNN."""
    if x[FI["d_hum"]] > 3.0:
        return "cooking"
    if x[FI["peak_rel"]] < 0.15:
        return "normal"
    if x[FI["ex_r10"]] > 2.2:
        return "pollution"
    return "smoking"


def fit_knn(evs, k, max_dist=None, meta=None):
    X = [v for e in evs for v in e["snaps"].values()]
    y = [e["label"] for e in evs for _ in e["snaps"]]
    return KnnModel.fit(X, y, k=k, max_dist=max_dist, meta=meta)


def loeo(evs, k):
    """Leave-one-event-out: mỗi sự kiện -> {pred_by_age, nearest_lock}."""
    out = []
    for i, e in enumerate(evs):
        rest = [o for j, o in enumerate(evs) if j != i]
        if len({o["label"] for o in rest}) < 1:
            continue
        m = fit_knn(rest, k)
        preds = {a: m.predict(v)[0] for a, v in sorted(e["snaps"].items())}
        out.append({"true": e["label"], "preds": preds,
                    "nearest_lock": m.predict(lock_vec(e))[1]["nearest_dist"]})
    return out


def latency(preds, true):
    """Tuổi sự kiện (giây) từ lúc nhãn đúng tới hết giây khoá; None nếu lúc khoá sai."""
    ages = sorted(preds)
    if preds[ages[-1]] != true:
        return None
    a = ages[-1]
    for age in reversed(ages):
        if preds[age] != true:
            break
        a = age
    return a


def confusion(pairs):
    m = np.zeros((len(LABELS), len(LABELS)), dtype=int)
    for t, p in pairs:
        if p in LABELS:
            m[LABELS.index(t), LABELS.index(p)] += 1
    return m.tolist()


def shape_loeo(evs, k, band, ages=None):
    """Leave-one-event-out cho kNN-DTW: đoán ở mỗi tuổi bằng đúng phần đường cong đã có."""
    out = []
    for i, e in enumerate(evs):
        rest = [o for j, o in enumerate(evs) if j != i]
        m = ShapeKnnModel.fit([o["shape"] for o in rest], [o["label"] for o in rest], k=k, band=band)
        use = sorted(e["shape_len"]) if ages is None else [a for a in sorted(e["shape_len"]) if a in ages]
        use = use or [max(e["shape_len"])]
        preds = {a: m.predict(e["shape"][:e["shape_len"][a]])[0] for a in use}
        nearest = m.predict(e["shape"])[1]["nearest_dist"]
        out.append({"true": e["label"], "preds": preds, "nearest_lock": nearest})
    return out


def summarize(res, floor):
    """Chỉ số LOEO + ngưỡng "chưa gặp" = max(floor, 1,5 × p95 khoảng cách tới mẫu gần nhất)."""
    lock_pairs = [(r["true"], r["preds"][max(r["preds"])]) for r in res]
    first_pairs = [(r["true"], r["preds"][min(r["preds"])]) for r in res]
    lat = {}
    for lab in LABELS:
        v = [latency(r["preds"], lab) for r in res if r["true"] == lab]
        v = [x for x in v if x is not None]
        if v:
            lat[lab] = float(np.median(v))
    near = np.array([r["nearest_lock"] for r in res])
    max_dist = float(max(floor, 1.5 * np.percentile(near, 95))) if len(near) else None
    return {"lock_accuracy": acc(lock_pairs), "first_guess_accuracy": acc(first_pairs),
            "confusion_lock": confusion(lock_pairs), "latency_median_age_s": lat}, max_dist


def tree_loeo(evs):
    from sklearn.tree import DecisionTreeClassifier
    pairs = []
    for i, e in enumerate(evs):
        rest = [o for j, o in enumerate(evs) if j != i]
        X = [v for o in rest for v in o["snaps"].values()]
        y = [o["label"] for o in rest for _ in o["snaps"]]
        t = DecisionTreeClassifier(max_depth=3, random_state=0).fit(X, y)
        pairs.append((e["label"], str(t.predict([lock_vec(e)])[0])))
    return pairs


def old_rf_pairs(evs, rows, path=MODELS / "source_rf.json"):
    """RandomForest cũ (cửa sổ 30 phút, học trên mô phỏng) tại đúng giây khoá của sự kiện."""
    if not path.exists():
        return None
    from aiot.source_features import WINDOW_S, buffer_from_rows, compute_features
    from aiot.source_model import load_model
    try:
        rf = load_model(path)
    except Exception:
        return None
    ts = np.array([r[0] for r in rows])
    pairs = []
    for e in evs:
        now = e["start"] + max(e["snaps"])
        lo, hi = np.searchsorted(ts, now - WINDOW_S + 1), np.searchsorted(ts, now, side="right")
        f = compute_features(buffer_from_rows(rows[lo:hi], now))
        pairs.append((e["label"], rf.predict(f)[0] if f is not None else None))
    return pairs


def acc(pairs):
    return round(float(np.mean([t == p for t, p in pairs])), 3) if pairs else None


def plot_map(evs, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    colors = {"normal": "#7f8c8d", "cooking": "#e67e22", "pollution": "#8e44ad", "smoking": "#c0392b"}
    fig, ax = plt.subplots(figsize=(6, 4.5), dpi=120)
    for lab in LABELS:
        pts = np.array([lock_vec(e) for e in evs if e["label"] == lab])
        if len(pts):
            ax.scatter(pts[:, FI["d_hum"]], pts[:, FI["ex_r10"]], s=40, c=colors[lab],
                       label=lab, alpha=0.8, edgecolors="white")
    ax.set_xlabel("Độ ẩm tăng so với nền (%)")
    ax.set_ylabel("ΔPM10 / ΔPM2.5 (hạt thô)")
    ax.set_title("Bản đồ sự kiện (vector lúc khoá nhãn)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return True


# ---------------- chạy ----------------
def train(rows, labels, *, k=3, shape_k=1, band=5, cfg: EventCfg = EventCfg(),
          out_dir: Path = MODELS, rf_path: Path = MODELS / "source_rf.json"):
    events = label_events(extract_events(rows, cfg), labels)
    unlabeled = [e for e in events if e["label"] is None]
    evs = [e for e in events if e["label"] is not None]
    counts = {lab: sum(e["label"] == lab for e in evs) for lab in LABELS}
    if len([c for c in counts.values() if c]) < 2:
        raise SystemExit(f"Cần sự kiện có nhãn của ít nhất 2 loại, đang có {counts}")

    # khoảng có nhãn (không phải normal) mà máy trạng thái không thấy sự kiện nào
    missed = []
    for r in labels.itertuples(index=False):
        if r.label != "normal" and not any(
                e["start"] <= int(r.end_ts) and e["end"] >= int(r.start_ts) for e in events):
            missed.append({"start_ts": int(r.start_ts), "end_ts": int(r.end_ts), "label": r.label})

    knn_sum, max_dist = summarize(loeo(evs, k), floor=2.0)
    shape_sum, shape_max = summarize(shape_loeo(evs, shape_k, band), floor=0.5)
    summary = {
        "trained_at": int(time.time()), "k": k, "shape_k": shape_k, "band": band,
        "max_dist": max_dist, "shape_max_dist": shape_max,
        "events": counts, "unlabeled_events": len(unlabeled), "missed_intervals": missed,
        "cfg": {"classify_from_s": cfg.classify_from_s, "lock_after_s": cfg.lock_after_s,
                "pre_s": cfg.pre_s},
        "loeo": {
            "shape_dtw_knn": shape_sum,
            "summary_knn": knn_sum,
            "rule_accuracy": acc([(e["label"], rule_label(lock_vec(e))) for e in evs]),
            "tree_d3_accuracy": acc(tree_loeo(evs)) if len(evs) > 2 else None,
        },
        "labels": list(LABELS),
    }
    rf = old_rf_pairs(evs, rows, rf_path)
    if rf is not None:
        summary["loeo"]["old_rf_sim_accuracy"] = acc(rf)

    out_dir.mkdir(parents=True, exist_ok=True)
    model = fit_knn(evs, k, max_dist=max_dist,
                    meta={"trained_at": summary["trained_at"], "events": counts})
    save_model(model, out_dir / "source_knn.json")
    shape_model = ShapeKnnModel.fit([e["shape"] for e in evs], [e["label"] for e in evs],
                                    k=shape_k, band=band, max_dist=shape_max,
                                    meta={"trained_at": summary["trained_at"], "events": counts})
    save_model(shape_model, out_dir / "source_shape.json")
    with open(out_dir / "source_knn_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    summary["map_png"] = plot_map(evs, out_dir / "source_knn_map.png")
    return shape_model, summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--db", type=Path, help="DB SQLite có fast_readings + source_labels")
    p.add_argument("--fast", type=Path, help="fast_readings.csv (mặc định training/data)")
    p.add_argument("--labels", type=Path, help="CSV start_ts,end_ts,label")
    p.add_argument("--k", type=int, default=3, help="k của kNN trên 8 số tóm tắt")
    p.add_argument("--shape-k", type=int, default=1, help="k của kNN-DTW trên đường cong")
    p.add_argument("--band", type=int, default=5, help="cửa sổ DTW (giây)")
    p.add_argument("--out", type=Path, default=MODELS)
    a = p.parse_args(argv)
    rows, labels = load_data(a.db, a.fast, a.labels)
    print(f"{len(rows)} mẫu 1 giây, {len(labels)} khoảng nhãn")
    _, s = train(rows, labels, k=a.k, shape_k=a.shape_k, band=a.band, out_dir=a.out)
    print(f"Sự kiện có nhãn: {s['events']} · không nhãn: {s['unlabeled_events']} · "
          f"khoảng bị bỏ sót: {len(s['missed_intervals'])}")
    lo = s["loeo"]
    for name, key in (("kNN-DTW đường cong", "shape_dtw_knn"), ("kNN 8 số tóm tắt", "summary_knn")):
        r = lo[key]
        print(f"{name} (leave-one-event-out): đúng lúc khoá {r['lock_accuracy']}, "
              f"lần đoán đầu {r['first_guess_accuracy']}, trễ trung vị {r['latency_median_age_s']}")
    print(f"Đối chứng: luật {lo['rule_accuracy']} · cây d3 {lo['tree_d3_accuracy']} · "
          f"RF mô phỏng cũ {lo.get('old_rf_sim_accuracy')}")
    print(f"Đã ghi {a.out / 'source_shape.json'}, {a.out / 'source_knn.json'} và "
          f"{a.out / 'source_knn_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
