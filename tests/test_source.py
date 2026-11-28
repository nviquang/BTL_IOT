"""Bộ nhận diện nguồn bụi: làm mượt, bảng hành động, luật quạt/còi theo nhãn AI, controller
chạy mô hình logistic regression (aiot/source_model.py)."""
import json
from pathlib import Path

import numpy as np
import pytest

from aiot.alarm import alarm_reason
from aiot.config import BuzzerCfg, ControlCfg, SourceCfg
from aiot.control import ControlState, Event, Inputs, decide, fan_on
from aiot.controller import SOURCE_KEY, load_source_model, step
from aiot.db import RawSample, connect, get_state, init_schema, insert_fast_many, insert_raw
from aiot.source_model import LABELS, LogRegModel, SourceState, actions, save_model, smooth
from aiot.source_window import WINDOW_FEATURES

CFG = ControlCfg()
MODEL_FILE = Path(__file__).resolve().parent.parent / "models" / "source_logreg.json"


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
    st = smooth(SourceState(), P["pollution"], 0)
    assert st.label == "pollution"
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
    assert actions(None) == (None, None)


# ---------------- luật quạt và còi theo nhãn ----------------
def ai_inp(source, now=1000, danger=None):
    return Inputs(now=now, last_sample_ts=now, danger_level=danger, source=source)


def test_cooking_turns_fan_on_immediately():
    st, ev = decide(ControlState(), ai_inp("cooking"), CFG)
    assert st.mode == "PROTECT" and Event("fan_on", "ai_cooking") in ev


def test_pollution_turns_fan_on():
    st, ev = decide(ControlState(), ai_inp("pollution"), CFG)
    assert st.mode == "PROTECT" and Event("fan_on", "ai_pollution") in ev


def test_normal_label_with_danger_level_keeps_fan_off():
    """Nền cao nhưng ổn định: AI nói bình thường, quạt không tự bật (đánh đổi của bản demo)."""
    st, ev = decide(ControlState(), ai_inp("normal", danger=900.0), CFG)
    assert st.mode == "NORMAL" and not fan_on(st, 1000)
    assert [e.kind for e in ev] == ["danger"]


def test_protect_exits_on_normal_after_min_state():
    st = ControlState(mode="PROTECT", entered_at=1000)
    st, ev = decide(st, ai_inp("normal", now=1000 + CFG.min_state_s - 1), CFG)
    assert st.mode == "PROTECT" and ev == []
    st, ev = decide(st, ai_inp("normal", now=1000 + CFG.min_state_s), CFG)
    assert st.mode == "NORMAL" and Event("fan_off", "ai_normal") in ev


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
    assert alarm_reason(10.0, b, source="smoking") is None   # nhãn cũ không còn
    assert alarm_reason(10.0, b, source="pollution") == "pollution"
    assert alarm_reason(900.0, b, source="cooking") is None
    assert alarm_reason(900.0, b) == "pm25"          # không có nhãn: luật cũ


# ---------------- controller ----------------
class StubModel:
    classes = LABELS
    name = "stub"

    def __init__(self, label):
        self.p = np.array([0.9 if c == label else 0.05 for c in LABELS])
        self.featurize_calls = 0

    def featurize(self, buf):
        self.featurize_calls += 1
        return np.zeros(3)

    def predict_proba(self, x):
        return self.p

    def anomaly_score(self, p):
        return float(1 - p[0])


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
                                              ("normal", False, [])])
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
    details = [d for (d,) in conn.execute("SELECT detail FROM events WHERE kind = 'source_change'")]
    assert details == [f"{label} p=0.90"]


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


def test_step_uses_model_featurize_and_logs_anomaly(conn):
    now = 100000
    feed(conn, now, pm25=40.0)
    m = StubModel("pollution")
    step(conn, Relay(), CFG, now, monotonic=lambda: 1e6, buzzer=Buzzer(), bcfg=BuzzerCfg(),
         model=m, scfg=SourceCfg())
    assert m.featurize_calls == 1
    (proba,) = conn.execute("SELECT proba FROM source_predictions").fetchone()
    assert json.loads(proba)["anomaly"] == pytest.approx(0.95)


def small_logreg(tmp_path):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    from aiot.source_model import spec_from_sklearn
    rng = np.random.default_rng(0)
    X = rng.normal(size=(300, len(WINDOW_FEATURES)))
    y = rng.integers(0, 3, 300)
    sc = StandardScaler().fit(X)
    lr = LogisticRegression(max_iter=500).fit(sc.transform(X), y)
    path = tmp_path / "lr.json"
    save_model(LogRegModel(spec_from_sklearn(sc, lr, [LABELS[c] for c in lr.classes_])), path)
    return path


def test_load_source_model_and_step_with_logreg(conn, tmp_path):
    model = load_source_model(SourceCfg(model_path=str(small_logreg(tmp_path))))
    assert isinstance(model, LogRegModel)
    now = 100000
    feed(conn, now, pm25=40.0, n=600)
    step(conn, Relay(), CFG, now, monotonic=lambda: 1e6, buzzer=Buzzer(), bcfg=BuzzerCfg(),
         model=model, scfg=SourceCfg())
    assert conn.execute("SELECT model FROM source_predictions").fetchone() == ("source_logreg",)


def test_load_source_model_missing_or_old_file_returns_none(tmp_path):
    assert load_source_model(SourceCfg(model_path=str(tmp_path / "none.json"))) is None
    old = tmp_path / "rf.json"
    old.write_text(json.dumps({"format": "aiot-rf-v1", "trees": []}), encoding="utf-8")
    assert load_source_model(SourceCfg(model_path=str(old))) is None


@pytest.mark.skipif(not MODEL_FILE.exists(), reason="chưa huấn luyện models/source_logreg.json")
@pytest.mark.parametrize("level", [20.0, 170.0])
def test_trained_model_flat_room_is_normal(conn, level):
    """Mô hình đã huấn luyện: phòng để yên 30 phút, nền thấp hay cao, đều ra bình thường."""
    model = load_source_model(SourceCfg(model_path=str(MODEL_FILE)))
    now = 100000
    feed(conn, now, pm25=level, n=1800)
    relay = Relay()
    for k in range(3):
        step(conn, relay, CFG, now + 5 * k, monotonic=lambda: 1e6, buzzer=Buzzer(),
             bcfg=BuzzerCfg(), model=model, scfg=SourceCfg())
    assert json.loads(get_state(conn, SOURCE_KEY))["label"] == "normal"
    assert relay.calls[-1] is False


def test_window_ends_at_latest_stored_sample(conn):
    """collector commit fast_readings mỗi 5 s: lúc controller chạy, 4 giây cuối có thể chưa có.
    Cửa sổ phải kết thúc ở mẫu mới nhất trong DB, nên khối mới nhất không rỗng."""
    seen = {}

    class Spy(StubModel):
        def featurize(self, buf):
            seen["last"] = buf["pm25"][-1]
            seen["miss"] = int(np.isnan(buf["pm25"][-5:]).sum())
            return np.zeros(3)

    now = 100000
    feed(conn, now - 4, pm25=40.0, n=600)       # mẫu mới nhất lúc now - 4
    step(conn, Relay(), CFG, now, monotonic=lambda: 1e6, buzzer=Buzzer(), bcfg=BuzzerCfg(),
         model=Spy("normal"), scfg=SourceCfg())
    assert seen == {"last": 40.0, "miss": 0}
