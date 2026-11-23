"""Bộ nhận diện nguồn bụi: đặc trưng, suy luận JSON, làm mượt, luật quạt/còi theo nhãn AI."""
import json

import numpy as np
import pytest

from aiot.alarm import alarm_reason
from aiot.config import BuzzerCfg, ControlCfg, SourceCfg
from aiot.control import ControlState, Event, Inputs, decide, fan_on
from aiot.controller import SOURCE_KEY, step
from aiot.db import RawSample, connect, get_state, init_schema, insert_fast_many, insert_raw
from aiot.notifier import event_message
from aiot.source_features import FEATURES, WINDOW_S, buffer_from_rows, compute_features
from aiot.source_model import (FORMAT, LABELS, SourceModel, SourceState, actions, load_model,
                               smooth)

CFG = ControlCfg()


def flat_buffer(pm25=20.0, hum=70.0):
    return {"pm1": np.full(WINDOW_S, pm25 * 0.65), "pm25": np.full(WINDOW_S, pm25),
            "pm10": np.full(WINDOW_S, pm25 * 1.2), "temp": np.full(WINDOW_S, 26.0),
            "hum": np.full(WINDOW_S, hum), "lux": np.full(WINDOW_S, 220.0)}


# ---------------- đặc trưng ----------------
def test_features_none_without_pm():
    buf = flat_buffer()
    buf["pm25"][-60:] = np.nan
    assert compute_features(buf) is None


def test_features_flat_background():
    f = dict(zip(FEATURES, compute_features(flat_buffer(20.0))))
    assert abs(f["exc_rel5"]) < 1e-9 and f["has_exc"] == 0.0
    assert f["log_base"] == pytest.approx(np.log1p(20.0))
    assert f["r1_60"] == pytest.approx(13.0 / 21.0)


def test_features_spike_and_excess_ratio():
    buf = flat_buffer(170.0)
    buf["pm25"][-20:] += 600
    buf["pm1"][-20:] += 600 * 0.3
    buf["pm10"][-20:] += 600 * 1.9
    f = dict(zip(FEATURES, compute_features(buf)))
    assert f["exc_rel5"] > 3 and f["has_exc"] == 1.0
    assert f["er1"] == pytest.approx(0.3, abs=0.02)
    assert f["er10"] == pytest.approx(1.9, abs=0.02)


def test_features_humidity_rise():
    buf = flat_buffer(30.0)
    buf["hum"][-200:] += 10
    f = dict(zip(FEATURES, compute_features(buf)))
    assert f["dh300"] == pytest.approx(10.0) and f["dh_base"] == pytest.approx(10.0)


def test_buffer_from_rows_places_by_second():
    rows = [(1000, {"pm25": 5.0}), (995, {"pm25": 7.0, "hum": 60.0})]
    buf = buffer_from_rows(rows, 1000, window_s=10)
    assert buf["pm25"][-1] == 5.0 and buf["pm25"][-6] == 7.0 and np.isnan(buf["pm25"][-2])
    assert buf["hum"][-6] == 60.0


# ---------------- mô hình JSON ----------------
def stump_spec(feature="exc_rel5", thr=1.0, left=(1, 0, 0, 0), right=(0, 0, 0, 1)):
    """Một cây gốc + 2 lá: feature <= thr -> left, ngược lại -> right."""
    return {"format": FORMAT, "name": "stub", "classes": list(LABELS), "features": list(FEATURES),
            "trees": [{"left": [1, -1, -1], "right": [2, -1, -1],
                       "feature": [FEATURES.index(feature), 0, 0], "threshold": [thr, -2, -2],
                       "value": [[0.25] * 4, list(left), list(right)]}]}


def test_model_predicts_by_tree(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(stump_spec()))
    m = load_model(path)
    x = np.zeros(len(FEATURES))
    assert m.predict(x)[0] == "normal"
    x[FEATURES.index("exc_rel5")] = 5
    assert m.predict(x)[0] == "smoking"


def test_model_rejects_wrong_features():
    spec = stump_spec()
    spec["features"] = spec["features"][:-1]
    with pytest.raises(ValueError):
        SourceModel(spec)


def test_json_matches_sklearn(tmp_path):
    sk = pytest.importorskip("sklearn.ensemble")
    from training.train_source import export_forest
    rng = np.random.default_rng(0)
    X = rng.normal(size=(400, len(FEATURES)))
    y = (X[:, 0] > 0).astype(int) + 2 * (X[:, 1] > 0.5).astype(int)
    rf = sk.RandomForestClassifier(n_estimators=7, max_depth=5, random_state=0).fit(X, y)
    m = export_forest(rf, tmp_path / "rf.json", {})
    for x in X[:50]:
        assert np.allclose(m.predict_proba(x), rf.predict_proba(x[None])[0], atol=1e-5)


# ---------------- làm mượt ----------------
P = {lab: tuple(0.85 if c == lab else 0.05 for c in LABELS) for lab in LABELS}


def test_smooth_switches_after_votes():
    st = SourceState()
    st = smooth(st, P["normal"], 0)
    assert st.label == "normal"
    st = smooth(st, P["cooking"], 5)
    assert st.label == "normal"                 # TB (normal, cooking) chưa đủ 0,6
    st = smooth(st, P["cooking"], 10)
    st = smooth(st, P["cooking"], 15)
    assert st.label == "cooking" and st.since == 15


def test_smooth_resets_on_missing_or_gap():
    st = smooth(SourceState(), P["smoking"], 0)
    assert st.label == "smoking"
    assert smooth(st, None, 5).label is None
    st2 = smooth(st, P["normal"], 100)          # ngắt quãng > 30 s: làm lại từ đầu
    assert st2.label == "normal" and len(st2.history) == 1


def test_source_state_json_roundtrip_and_corrupt():
    st = smooth(SourceState(), P["pollution"], 7)
    assert SourceState.from_json(st.to_json()) == st
    assert SourceState.from_json("hỏng") == SourceState()


def test_action_table():
    assert actions("normal") == (False, False)
    assert actions("cooking") == (True, False)
    assert actions("pollution") == (True, True)
    assert actions("smoking") == (False, True)
    assert actions(None) == (None, None)


# ---------------- luật quạt và còi theo nhãn ----------------
def ai_inp(source, now=1000, danger=None):
    return Inputs(now=now, last_sample_ts=now, danger_level=danger, source=source)


def test_cooking_turns_fan_on_immediately():
    st, ev = decide(ControlState(), ai_inp("cooking"), CFG)
    assert st.mode == "PROTECT" and Event("fan_on", "ai_cooking") in ev


def test_smoking_with_danger_level_keeps_fan_off():
    st, ev = decide(ControlState(), ai_inp("smoking", danger=900.0), CFG)
    assert st.mode == "NORMAL" and not fan_on(st, 1000)
    assert [e.kind for e in ev] == ["danger"]


def test_protect_exits_on_smoking_after_min_state():
    st = ControlState(mode="PROTECT", entered_at=1000)
    st, ev = decide(st, ai_inp("smoking", now=1000 + CFG.min_state_s - 1), CFG)
    assert st.mode == "PROTECT" and ev == []
    st, ev = decide(st, ai_inp("smoking", now=1000 + CFG.min_state_s), CFG)
    assert st.mode == "NORMAL" and Event("fan_off", "ai_smoking") in ev


def test_override_still_wins_in_ai_mode():
    st = ControlState(override_fan=True, override_until=5000)
    st, _ = decide(st, ai_inp("normal"), CFG)
    assert fan_on(st, 1000) is True


def test_stale_data_holds_state_in_ai_mode():
    st = ControlState(mode="PROTECT", entered_at=0)
    st, ev = decide(st, Inputs(now=1000, last_sample_ts=500, source="normal"), CFG)
    assert st.mode == "PROTECT" and [e.kind for e in ev] == ["sensor_fault"]


def test_alarm_by_source():
    b = BuzzerCfg()
    assert alarm_reason(10.0, b, source="smoking") == "smoking"
    assert alarm_reason(10.0, b, source="pollution") == "pollution"
    assert alarm_reason(900.0, b, source="cooking") is None
    assert alarm_reason(900.0, b) == "pm25"          # không có nhãn: luật cũ


def test_source_change_message():
    msg, urgent = event_message(Event("source_change", "smoking p=0.91"), ControlState(), None, 1000)
    assert "khói thuốc" in msg and "0,91" in msg and not urgent


# ---------------- controller ----------------
class StubModel:
    classes = LABELS
    name = "stub"

    def __init__(self, label):
        self.p = np.array([0.9 if c == label else 0.1 / 3 for c in LABELS])

    def predict_proba(self, x):
        return self.p


class Relay:
    def __init__(self):
        self.calls = []

    def set(self, on):
        self.calls.append(on)


class Buzzer:
    def __init__(self):
        self.calls = []

    def burst(self, *a):
        self.calls.append("burst")

    def silence(self):
        self.calls.append("silence")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    yield c
    c.close()


def feed(conn, now, pm25=150.0, n=300):
    insert_fast_many(conn, [(t, {"pm1": pm25 * .6, "pm25": pm25, "pm10": pm25 * 1.2,
                                 "temp": 26.0, "hum": 70.0, "lux": 200.0}, True)
                            for t in range(now - n + 1, now + 1)])
    for t in range(now - n + 1, now + 1, 10):
        insert_raw(conn, RawSample(t, None, pm25, None, None, None, None, True))


@pytest.mark.parametrize("label, fan, buzz", [("cooking", True, []), ("pollution", True, ["burst"]),
                                              ("smoking", False, ["burst"]), ("normal", False, [])])
def test_step_drives_fan_and_buzzer_by_label(conn, label, fan, buzz):
    now = 100000
    feed(conn, now, pm25=40.0)
    relay, buzzer = Relay(), Buzzer()
    step(conn, relay, CFG, now, monotonic=lambda: 1e6, buzzer=buzzer, bcfg=BuzzerCfg(),
         model=StubModel(label), scfg=SourceCfg())
    assert relay.calls[-1] is fan
    assert buzzer.calls == buzz
    assert json.loads(get_state(conn, SOURCE_KEY))["label"] == label
    row = conn.execute("SELECT label, raw_label, model FROM source_predictions").fetchone()
    assert row == (label, label, "stub")
    kinds = [k for (k,) in conn.execute("SELECT kind FROM events")]
    assert "source_change" in kinds
    outbox = [k for (k,) in conn.execute("SELECT kind FROM outbox")]
    assert f"source_{label}" in outbox


def test_step_falls_back_to_pm_rules_without_fast_data(conn):
    now = 100000
    for t in range(now - 300, now + 1, 10):
        insert_raw(conn, RawSample(t, None, 200.0, None, None, None, None, True))
    relay, buzzer = Relay(), Buzzer()
    step(conn, relay, CFG, now, monotonic=lambda: 1e6, buzzer=buzzer, bcfg=BuzzerCfg(),
         model=StubModel("normal"), scfg=SourceCfg())
    assert relay.calls[-1] is True              # luật cũ: khẩn cấp -> quạt bật
    assert buzzer.calls == ["burst"]
    assert json.loads(get_state(conn, SOURCE_KEY))["label"] is None


def test_step_model_error_falls_back(conn):
    class Broken(StubModel):
        def predict_proba(self, x):
            raise RuntimeError("hỏng")
    now = 100000
    feed(conn, now, pm25=200.0)
    relay = Relay()
    step(conn, relay, CFG, now, monotonic=lambda: 1e6, model=Broken("normal"), scfg=SourceCfg())
    assert relay.calls[-1] is True


# ---------------- mô phỏng ----------------
def test_simulator_has_all_labels_and_is_reproducible():
    from training.simulate_sources import simulate
    a = simulate(30, seed=3)
    b = simulate(30, seed=3)
    labels = np.concatenate([arr["label"] for arr, _ in a])
    assert set(np.unique(labels)) == {0, 1, 2, 3}
    assert np.array_equal(a[0][0]["pm25"], b[0][0]["pm25"], equal_nan=True)
    arr = a[0][0]
    ok = np.isfinite(arr["pm25"])
    assert np.all(arr["pm1"][ok] <= arr["pm25"][ok]) and np.all(arr["pm10"][ok] >= arr["pm25"][ok])
