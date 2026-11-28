"""Nhận diện nguồn bụi theo sự kiện (profile demo): nền phòng, máy trạng thái, đặc trưng, kNN."""
import json

import numpy as np
import pytest

from aiot.source_event import (EVENT_FEATURES, SHAPE_CHANNELS, EventCfg, TrackerState, calibrate,
                               features, run, shape, update)
from aiot.source_model import (FORMAT_KNN, KnnModel, ShapeKnnModel, dtw, load_model,
                               save_model)

BASE = 160.0


def sample(pm25=BASE, r1=0.61, r10=1.18, hum=72.0, temp=27.0, coarse=0.02, ex=None):
    """Một mẫu 1 giây. ex = (ΔPM2.5, tỉ lệ PM1, tỉ lệ PM10, tỉ lệ hạt thô) cho phần vượt nền."""
    p1, p10 = BASE * r1, BASE * r10
    n03, nc = BASE * 60.0, BASE * 60.0 * coarse
    p25 = BASE
    if ex is not None:
        d, er1, er10, ec = ex
        p25 += d
        p1 += er1 * d
        p10 += er10 * d
        n03 += 60.0 * d
        nc += 60.0 * d * ec
    if pm25 != BASE and ex is None:
        p25 = pm25
    return {"pm1": p1, "pm25": p25, "pm10": p10, "n03": n03, "n50": nc * 0.7, "n100": nc * 0.3,
            "n05": 0.0, "n10": 0.0, "n25": 0.0, "temp": temp, "hum": hum, "lux": 220.0}


def prop_rows(kind, t0=0, rng=None, idle_before=120, idle_after=120):
    """[(ts, mẫu)] cho một lần làm đạo cụ: nền, sự kiện, về nền. Gần giống tín hiệu kỳ vọng."""
    rng = rng or np.random.default_rng(0)
    out = []
    t = t0
    for _ in range(idle_before):
        out.append((t, sample(hum=72 + rng.normal(0, 0.3), ex=(rng.normal(0, 2), 0.6, 1.2, 0.02))))
        t += 1
    dur = 60
    for i in range(dur):
        noise = rng.normal(0, 2)
        if kind == "cooking":       # hơi nước: ẩm tăng mạnh, PM tăng chậm
            up = min(i / 20, 1.0)
            s = sample(hum=72 + 12 * up, temp=27 + 1.0 * up,
                       ex=(40 * up * rng.uniform(0.9, 1.1) + noise, 0.75, 1.15, 0.02))
        elif kind == "smoking":     # nhang: gai nhọn, hạt mịn
            spike = 600 * np.exp(-((i % 25) - 5) ** 2 / 6.0)
            s = sample(hum=72, ex=(spike + noise, 0.35, 1.8, 0.03))
        elif kind == "pollution":   # bụi phấn: một đợt hạt thô
            wave = 150 * np.exp(-((i - 15) / 12.0) ** 2)
            s = sample(hum=72, ex=(wave + noise, 0.2, 3.0, 0.5))
        else:                       # normal: chỉ nhiễu
            s = sample(hum=72, ex=(noise, 0.6, 1.2, 0.02))
        out.append((t, s))
        t += 1
    for _ in range(idle_after):
        out.append((t, sample(hum=72, ex=(rng.normal(0, 2), 0.6, 1.2, 0.02))))
        t += 1
    return out


def labeler(seen):
    def classify(st):
        x = features(st)
        seen.append(np.array(x))
        if x[EVENT_FEATURES.index("d_hum")] > 5:
            return "cooking", 1.0
        if x[EVENT_FEATURES.index("ex_r10")] > 2.4:
            return "pollution", 1.0
        return "smoking", 1.0
    return classify


# ---------------- máy trạng thái ----------------
def test_flat_background_stays_idle_and_learns_base():
    st = TrackerState()
    for t in range(600):
        st = update(st, t, sample())
    assert st.phase == "idle" and st.label == "normal" and st.n_events == 0
    assert st.base["pm25"] == pytest.approx(BASE)
    assert features(st) is None


def test_base_follows_slow_drift():
    st = TrackerState()
    for t in range(1500):                           # nền tăng dần 160 -> 175 trong 25 phút
        st = update(st, t, sample(pm25=BASE + 15 * t / 1500))
    assert st.phase == "idle" and st.base["pm25"] > BASE + 8


@pytest.mark.parametrize("kind,label", [("smoking", "smoking"), ("cooking", "cooking"),
                                        ("pollution", "pollution")])
def test_event_lifecycle(kind, label):
    seen = []
    cfg = EventCfg()
    st, labels, phases = TrackerState(), [], []
    for ts, s in prop_rows(kind):
        st = update(st, ts, s, labeler(seen), cfg)
        labels.append(st.label)
        phases.append(st.phase)
    assert st.n_events == 1, kind
    assert "event" in phases and "cooldown" in phases
    assert st.phase == "idle" and st.label == "normal"
    assert label in labels
    start = phases.index("event")
    first = next(i for i, lab in enumerate(labels) if lab == label)
    assert first - start <= cfg.classify_from_s + 2     # nhãn hiện ra trong khoảng 10 giây
    assert labels[start] is None                        # chưa đủ 8 giây: chưa đoán
    assert len(seen) == cfg.lock_after_s - cfg.classify_from_s + 1   # khoá nhãn ở giây 30


def test_features_separate_props():
    def feats(kind):
        st = TrackerState()
        out = None
        for ts, s in prop_rows(kind):
            st = update(st, ts, s)
            if st.phase == "event" and st.age_s == 30:
                out = dict(zip(EVENT_FEATURES, features(st)))
        return out

    smoke, cook, dust = feats("smoking"), feats("cooking"), feats("pollution")
    assert cook["d_hum"] > 8 and smoke["d_hum"] < 1 and dust["d_hum"] < 1
    assert smoke["rise_rate"] > 5 * cook["rise_rate"]
    assert smoke["spikiness"] >= 2 * dust["spikiness"] > 0
    assert dust["ex_r10"] > 2.5 > smoke["ex_r10"] > cook["ex_r10"]
    assert dust["coarse_frac"] > 0.3 > smoke["coarse_frac"]
    assert smoke["ex_r1"] == pytest.approx(0.35, abs=0.05)


def test_humidity_alone_triggers_event():
    st = TrackerState()
    for t in range(120):
        st = update(st, t, sample())
    for t in range(120, 130):
        st = update(st, t, sample(hum=72 + 6))
    assert st.phase == "event"
    f = dict(zip(EVENT_FEATURES, features(st)))
    assert f["d_hum"] == pytest.approx(6)
    assert f["ex_r1"] == 0.0 and f["ex_r10"] == 0.0     # không có bụi: không tính tỉ lệ hạt


def test_missing_pm_ignored_and_gap_cancels_event():
    st = TrackerState()
    for t in range(60):
        st = update(st, t, sample())
    st2 = update(st, 60, {"pm25": None, "hum": 99})
    assert st2 == st
    for t in range(60, 70):
        st = update(st, t, sample(pm25=400))
    assert st.phase == "event"
    st = update(st, 200, sample(pm25=400))              # mất mẫu 130 giây
    assert st.phase == "idle" and st.base["pm25"] == pytest.approx(BASE)


def test_long_event_rebaselines():
    cfg = EventCfg(max_event_s=120)
    st = TrackerState()
    for t in range(60):
        st = update(st, t, sample(), cfg=cfg)
    for t in range(60, 200):                            # nền nhảy hẳn lên 260
        st = update(st, t, sample(pm25=260), cfg=cfg)
    assert st.phase == "idle" and st.base["pm25"] == pytest.approx(260)


def test_unknown_label_does_not_lock():
    st = TrackerState()
    calls = []

    def unsure(st):
        calls.append(1)
        return None, 0.4
    for ts, s in prop_rows("smoking"):
        st = update(st, ts, s, unsure)
        assert st.locked is False
    assert len(calls) > 30                              # đoán lại tới hết sự kiện


def test_calibrate_and_json_roundtrip():
    st = run(TrackerState(), prop_rows("smoking")[:150], labeler([]))
    assert st.phase == "event"
    back = TrackerState.from_json(st.to_json())
    assert back == st
    st3 = update(back, 151, prop_rows("smoking")[151][1], labeler([]))
    assert st3.n_events == 1
    cal = calibrate(st, sample(pm25=200), ts=500)
    assert cal.phase == "idle" and cal.base["pm25"] == 200 and cal.n_events == 1
    assert TrackerState.from_json("{bad") == TrackerState()
    assert TrackerState.from_json(json.dumps({"phase": "zzz"})) == TrackerState()


# ---------------- kNN ----------------
def knn_toy(**kw):
    X = [[0, 0, 0, 0, 0, 10, 1, 0], [0, 0, 0, 0, 0, 11, 1, 0],
         [10, 4, .35, 1.8, 0, 0, 0, 4], [11, 4, .35, 1.8, 0, 0, 0, 5],
         [2, 1, .2, 3, .5, 0, 0, 1], [2, 1, .2, 3.1, .5, 0, 0, 1]]
    y = ["cooking", "cooking", "smoking", "smoking", "pollution", "pollution"]
    return KnnModel.fit(X, y, **kw)


def test_knn_predicts_and_reports_neighbors():
    m = knn_toy(k=3)
    label, info = m.predict([0, 0, 0, 0, 0, 10.5, 1, 0])
    assert label == "cooking" and info["votes"]["cooking"] == 2
    assert info["neighbors"][0]["label"] == "cooking"
    p = m.predict_proba([10.5, 4, .35, 1.8, 0, 0, 0, 4.5])
    assert p.sum() == pytest.approx(1.0) and p[3] == pytest.approx(2 / 3)


def test_knn_unknown_when_far():
    m = knn_toy(k=1, max_dist=1.0)
    assert m.predict([100, 100, 3, 10, 5, 50, 20, 60])[0] is None
    assert m.classify(None) == (None, None)
    assert m.classify([0, 0, 0, 0, 0, 10, 1, 0])[0] == "cooking"


def test_knn_teach_save_load(tmp_path):
    m = knn_toy(k=1, max_dist=1.0)
    x = [0, 0, 0, 0, 0, 0, 0, 0]
    m2 = m.add_example(x, "normal")
    assert m2.predict(x)[0] == "normal" and len(m2.y) == len(m.y) + 1
    assert np.allclose(m2.mean, m.mean)
    path = tmp_path / "knn.json"
    save_model(m2, path)
    back = load_model(path)
    assert isinstance(back, KnnModel) and back.y == m2.y and back.max_dist == 1.0
    with pytest.raises(ValueError):
        m.add_example(x, "fire")


def test_knn_rejects_bad_spec():
    spec = knn_toy().to_spec()
    with pytest.raises(ValueError):
        KnnModel({**spec, "features": spec["features"][:-1]})
    with pytest.raises(ValueError):
        KnnModel({**spec, "format": "aiot-rf-v1"})
    with pytest.raises(ValueError):
        KnnModel({**spec, "y": spec["y"][:-1]})
    assert spec["format"] == FORMAT_KNN


def test_controller_refuses_knn_model(tmp_path):
    from aiot.config import SourceCfg
    from aiot.controller import load_source_model
    path = tmp_path / "knn.json"
    save_model(knn_toy(), path)
    assert load_source_model(SourceCfg(model_path=str(path))) is None


# ---------------- huấn luyện demo (training/train_demo.py) ----------------
def breath_rows(t0, rng):
    """Thở gần cảm biến: ẩm tăng 4 % trong 12 giây, gần như không có bụi -> 'normal'."""
    out = [(t0 + i, sample(hum=72, ex=(rng.normal(0, 2), 0.6, 1.2, 0.02))) for i in range(120)]
    out += [(t0 + 120 + i, sample(hum=76, ex=(rng.normal(0, 2) + 3, 0.6, 1.2, 0.02)))
            for i in range(12)]
    out += [(t0 + 132 + i, sample(hum=72, ex=(rng.normal(0, 2), 0.6, 1.2, 0.02))) for i in range(120)]
    return out


def demo_dataset(n_each=4, seed=0):
    import pandas as pd
    rng = np.random.default_rng(seed)
    rows, labels, t = [], [], 1_800_000_000
    kinds = ["cooking", "smoking", "pollution", "normal"] * n_each
    rng.shuffle(kinds)
    for kind in kinds:
        part = breath_rows(t, rng) if kind == "normal" else prop_rows(kind, t, rng)
        rows += part
        labels.append((t + 110, t + 200, kind))
        t = part[-1][0] + 1
    return rows, pd.DataFrame(labels, columns=["start_ts", "end_ts", "label"])


def test_train_demo_end_to_end(tmp_path):
    pytest.importorskip("pandas")
    pytest.importorskip("sklearn")
    from training.train_demo import extract_events, label_events, train
    rows, labels = demo_dataset()
    evs = label_events(extract_events(rows), labels)
    assert len(evs) == 16 and all(e["label"] is not None for e in evs)
    model, s = train(rows, labels, out_dir=tmp_path, rf_path=tmp_path / "none.json")
    assert s["events"] == {"normal": 4, "cooking": 4, "pollution": 4, "smoking": 4}
    assert s["missed_intervals"] == [] and s["unlabeled_events"] == 0
    assert s["loeo"]["shape_dtw_knn"]["lock_accuracy"] == 1.0
    assert s["loeo"]["summary_knn"]["lock_accuracy"] == 1.0
    for f in ("source_shape.json", "source_knn.json", "source_knn_summary.json"):
        assert (tmp_path / f).exists()
    assert isinstance(load_model(tmp_path / "source_knn.json"), KnnModel)
    back = load_model(tmp_path / "source_shape.json")
    assert isinstance(back, ShapeKnnModel) and back.max_dist is not None
    assert len(back.seqs) == 16
    # mô hình đã xuất chạy được trong máy trạng thái trên dữ liệu mới
    st, got = TrackerState(), set()
    for ts, smp in prop_rows("pollution", 0, np.random.default_rng(99)):
        st = update(st, ts, smp, back.classify_event)
        got.add(st.label)
    assert "pollution" in got and st.label == "normal"


# ---------------- đường cong và kNN-DTW ----------------
def curve(kind, seed=0):
    st, out = TrackerState(), None
    for ts, smp in prop_rows(kind, 0, np.random.default_rng(seed)):
        st = update(st, ts, smp)
        if st.phase == "event":
            out = shape(st)
    return out


def test_shape_window_starts_before_trigger_and_stops_at_lock():
    cfg = EventCfg()
    st, lens = TrackerState(), {}
    for ts, smp in prop_rows("smoking"):
        st = update(st, ts, smp, cfg=cfg)
        if st.phase == "event":
            lens[st.age_s] = len(shape(st))
    assert lens[min(lens)] == cfg.pre_s                  # 10 giây trước + mẫu kích hoạt
    assert max(lens.values()) == cfg.pre_s + cfg.lock_after_s
    q = curve("smoking")
    assert q.shape[1] == len(SHAPE_CHANNELS)
    assert abs(q[0]).max() < 0.2                         # đầu cửa sổ còn ở nền
    assert q[:, SHAPE_CHANNELS.index("pm25_rel")].max() > 1.0
    assert shape(TrackerState()) is None


def test_shape_survives_json_roundtrip():
    st = run(TrackerState(), prop_rows("cooking")[:150])
    back = TrackerState.from_json(st.to_json())
    assert np.allclose(shape(back), shape(st))


def test_dtw_tolerates_time_shift_but_not_shape_change():
    t = np.arange(40, dtype=float)
    bump = np.exp(-((t - 15) / 4) ** 2)[:, None]
    shifted = np.exp(-((t - 18) / 4) ** 2)[:, None]
    spikes = (np.sin(t / 2) > 0.9).astype(float)[:, None]
    assert dtw(bump, bump) == 0.0
    euclid = float(np.sqrt(((bump - shifted) ** 2).sum(axis=1)).mean())
    assert dtw(bump, shifted) < 0.3 * euclid
    assert dtw(bump, spikes) > 3 * dtw(bump, shifted)


def test_shape_knn_separates_props_and_classifies_early():
    kinds = ["cooking", "smoking", "pollution"]
    m = ShapeKnnModel.fit([curve(k, s) for k in kinds for s in (1, 2, 3)],
                          [k for k in kinds for _ in range(3)], k=1)
    for k in kinds:
        q = curve(k, 7)
        assert m.predict(q)[0] == k
        assert m.predict(q[:20])[0] == k                 # mới 11 giây của sự kiện
    st, got = TrackerState(), []
    for ts, smp in prop_rows("pollution", 0, np.random.default_rng(8)):
        st = update(st, ts, smp, m.classify_event)
        got.append(st.label)
    assert "pollution" in got and st.label == "normal"


def test_shape_knn_unknown_teach_and_roundtrip(tmp_path):
    m = ShapeKnnModel.fit([curve("smoking", 1), curve("cooking", 1)], ["smoking", "cooking"],
                          k=1, max_dist=0.5)
    dust = curve("pollution", 1)
    assert m.predict(dust)[0] is None                    # chưa gặp bụi thô
    m2 = m.add_example(dust, "pollution")
    assert m2.predict(curve("pollution", 2))[0] == "pollution"
    path = tmp_path / "shape.json"
    save_model(m2, path)
    back = load_model(path)
    assert isinstance(back, ShapeKnnModel) and back.y == m2.y
    with pytest.raises(ValueError):
        ShapeKnnModel({**m.to_spec(), "channels": ["x"]})
