"""Test luật bật/tắt quạt: vào/thoát BẢO VỆ, khẩn cấp, dự báo, mất dữ liệu, ghi đè."""
import dataclasses
import json
import math

import pytest

from aiot.config import ControlCfg
from aiot.control import (
    ControlState,
    Event,
    Inputs,
    apply_override,
    decide,
    fan_on,
    inputs_from_series,
    rolling_mean,
    rolling_median,
)

CFG = ControlCfg()


def inp(now, **kw):
    """Inputs với mẫu mới nhất đúng lúc now (dữ liệu tươi) trừ khi chỉ định khác."""
    kw.setdefault("last_sample_ts", now)
    return Inputs(now=now, **kw)


def run(st, times, cfg=CFG, **kw):
    """Gọi decide tại từng thời điểm; giá trị trong kw có thể là hàm của t.
    Trả về trạng thái cuối và danh sách (t, state, events)."""
    log = []
    for t in times:
        args = {k: (v(t) if callable(v) else v) for k, v in kw.items()}
        st, events = decide(st, inp(t, **args), cfg)
        log.append((t, st, events))
    return st, log


def kinds(log):
    return [e.kind for _, _, events in log for e in events]


def simulate(pm, t_from, t_to, cfg=CFG, st=None, history_from=0, sample_s=10):
    """Mô phỏng đầy đủ: cảm biến cho mẫu mỗi sample_s giây (pm(t) trả None = mất mẫu),
    bộ điều khiển chạy mỗi loop_s giây, tính trung bình/trung vị từ chuỗi."""
    st = st or ControlState()
    series = []
    next_sample = history_from
    log = []
    for now in range(t_from, t_to + 1, cfg.loop_s):
        while next_sample <= now:
            v = pm(next_sample)
            if v is not None:
                series.append((next_sample, v))
            next_sample += sample_s
        st, events = decide(st, inputs_from_series(series, now, cfg), cfg)
        log.append((now, st, events))
    return st, log


def transitions(log, start_mode="NORMAL"):
    count, mode = 0, start_mode
    for _, st, _ in log:
        if st.mode != mode:
            count += 1
            mode = st.mode
    return count


PROTECT_SINCE_0 = ControlState(mode="PROTECT", entered_at=0)


# ---------- trung bình và trung vị trượt ----------

def test_rolling_mean_window_and_coverage():
    series = [(t, 10.0 if t <= 300 else 40.0) for t in range(0, 610, 10)]
    # cửa sổ (0, 600]: 30 mẫu =10 (10..300), 30 mẫu =40 (310..600)
    assert rolling_mean(series, 600, 600) == pytest.approx(25.0)
    # cần ít nhất 50% số mẫu kỳ vọng: 600 s / 10 s * 0.5 = 30
    assert rolling_mean(series[:29], 290, 600) is None
    assert rolling_mean(series[:30], 290, 600) == pytest.approx(10.0)
    assert rolling_mean([], 600, 600) is None


def test_danger_needs_three_samples():
    assert rolling_median([(950, 200.0), (960, 200.0)], 1000, 60) is None
    assert rolling_median([(950, 200.0), (960, 200.0), (970, 180.0)], 1000, 60) == 200.0
    # mẫu ngoài cửa sổ 60 giây không được tính
    assert rolling_median([(940, 200.0), (950, 200.0), (960, 200.0)], 1000, 60) is None


# ---------- vào BẢO VỆ ----------

def test_enter_after_5_min_above_55():
    st, log = run(ControlState(), list(range(0, 300, 20)) + [299], mean_enter=60.0)
    assert st.mode == "NORMAL"
    assert st.enter_since == 0
    assert kinds(log) == []
    st, events = decide(st, inp(300, mean_enter=60.0), CFG)
    assert st.mode == "PROTECT"
    assert st.entered_at == 300
    assert events == [Event("fan_on", "enter")]
    assert fan_on(st, 300) is True


def test_exactly_55_does_not_enter():
    st, log = run(ControlState(), range(0, 3601, 20), mean_enter=55.0)
    assert st.mode == "NORMAL"
    assert st.enter_since is None
    assert kinds(log) == []


def test_dip_resets_enter_timer():
    def mean(t):
        if t == 280:
            return 50.0     # tụt dưới ngưỡng một lần
        if t == 300:
            return None     # thiếu dữ liệu cũng xoá bộ đếm
        return 60.0
    st, log = run(ControlState(), range(0, 600, 20), mean_enter=mean)
    assert st.mode == "NORMAL"
    assert st.enter_since == 320
    st, events = decide(st, inp(620, mean_enter=60.0), CFG)
    assert st.mode == "PROTECT"
    assert events == [Event("fan_on", "enter")]


# ---------- thoát BẢO VỆ ----------

def test_exit_after_15_min_below_35_and_held_10_min():
    st, log = run(PROTECT_SINCE_0, range(0, 900, 20), mean_exit=30.0)
    assert st.mode == "PROTECT"
    assert st.exit_since == 0
    st, events = decide(st, inp(900, mean_exit=30.0), CFG)
    assert st.mode == "NORMAL"
    assert events == [Event("fan_off", "exit")]
    assert fan_on(st, 900) is False


def test_exactly_35_does_not_exit():
    st, log = run(PROTECT_SINCE_0, range(0, 3601, 20), mean_exit=35.0)
    assert st.mode == "PROTECT"
    assert st.exit_since is None
    assert kinds(log) == []


def test_exit_blocked_until_min_state():
    cfg = dataclasses.replace(CFG, exit_hold_s=60, min_state_s=600)
    st, log = run(PROTECT_SINCE_0, range(0, 600, 20), cfg=cfg, mean_exit=20.0)
    assert st.mode == "PROTECT"
    st, events = decide(st, inp(600, mean_exit=20.0), cfg)
    assert st.mode == "NORMAL"
    assert events == [Event("fan_off", "exit")]


# ---------- mức khẩn cấp ----------

def test_danger_enters_immediately_on_1min_median():
    st, log = simulate(lambda t: 20.0 if t < 1000 else 200.0, 400, 1200)
    on = [(t, events) for t, _, events in log if Event("fan_on", "danger") in events]
    assert len(on) == 1
    t_on, events = on[0]
    # 4/6 mẫu trong 1 phút ≥ 150 lần đầu ở t=1040; còn lâu mới tới enter_hold_s (300 s)
    assert t_on == 1040
    assert Event("danger") in events
    assert kinds(log).count("danger") == 1
    assert st.mode == "PROTECT"
    assert st.danger_active is True


def test_single_spike_does_not_trigger_danger():
    values = [20, 20, 900, 20, 20, 20]
    series = [(950 + 10 * i, float(v)) for i, v in enumerate(values)]
    assert rolling_median(series, 1000, 60) == 20.0
    st, events = decide(ControlState(), inputs_from_series(series, 1000, CFG), CFG)
    assert st.mode == "NORMAL"
    assert events == []


def test_danger_event_once_and_rearms():
    level = lambda t: 200.0 if t < 200 or t >= 400 else 100.0
    st, log = run(ControlState(), range(0, 600, 20), danger_level=level)
    assert kinds(log).count("danger") == 2
    assert kinds(log).count("fan_on") == 1


# ---------- dự báo ----------

def test_proactive_enters_on_fresh_forecast():
    st, events = decide(ControlState(), inp(10_000, forecast_3h=100.0, forecast_made_at=9_000), CFG)
    assert st.mode == "NORMAL"
    assert events == []
    st, events = decide(ControlState(), inp(10_000, forecast_3h=101.0, forecast_made_at=9_000), CFG)
    assert st.mode == "PROTECT"
    assert events == [Event("fan_on", "proactive")]


def test_stale_forecast_ignored():
    st, events = decide(ControlState(), inp(10_000, forecast_3h=300.0, forecast_made_at=10_000 - 5401), CFG)
    assert st.mode == "NORMAL"
    assert events == []
    # đúng 5400 giây vẫn còn tính là mới
    st, _ = decide(ControlState(), inp(10_000, forecast_3h=300.0, forecast_made_at=10_000 - 5400), CFG)
    assert st.mode == "PROTECT"
    # chưa có dự báo thì không có tác dụng
    st, _ = decide(ControlState(), inp(10_000, forecast_3h=300.0, forecast_made_at=None), CFG)
    assert st.mode == "NORMAL"


def test_exit_blocked_while_forecast_high():
    st, log = run(PROTECT_SINCE_0, range(0, 5401, 20),
                  mean_exit=20.0, forecast_3h=120.0, forecast_made_at=0)
    assert st.mode == "PROTECT"
    assert kinds(log) == []
    # dự báo hết hạn: không khí đã tốt đủ lâu nên thoát ngay ở vòng kế tiếp
    st, events = decide(st, inp(5420, mean_exit=20.0, forecast_3h=120.0, forecast_made_at=0), CFG)
    assert st.mode == "NORMAL"
    assert events == [Event("fan_off", "exit")]


# ---------- mất dữ liệu cảm biến ----------

@pytest.mark.parametrize("start", [
    ControlState(mode="NORMAL", enter_since=-200),
    ControlState(mode="PROTECT", entered_at=-5000, exit_since=-5000),
])
def test_sensor_gap_holds_state_and_faults_once(start):
    # mẫu cuối ở t=0; đúng 120 giây vẫn chưa coi là mất dữ liệu
    _, events = decide(start, inp(120, last_sample_ts=0), CFG)
    assert events == []
    # từ 140 giây: số liệu (cũ) đủ để đổi trạng thái nhưng không được dùng
    st, log = run(start, range(140, 1201, 20), last_sample_ts=0,
                  mean_enter=200.0, mean_exit=10.0, danger_level=200.0)
    assert st.mode == start.mode
    assert kinds(log) == ["sensor_fault"]
    assert log[0][2] == [Event("sensor_fault")]
    assert st.fault_active is True
    assert st.enter_since is None and st.exit_since is None
    assert transitions(log, start.mode) == 0


def test_recovery_restarts_timers():
    st, _ = run(ControlState(), range(0, 201, 20), mean_enter=80.0)
    assert st.enter_since == 0
    st, log = run(st, range(340, 501, 20), last_sample_ts=200, mean_enter=80.0)
    assert kinds(log) == ["sensor_fault"]
    st, events = decide(st, inp(520, mean_enter=80.0), CFG)
    assert events == [Event("sensor_recovered")]
    assert st.fault_active is False
    assert st.enter_since == 520
    st, log = run(st, range(540, 820, 20), mean_enter=80.0)
    assert st.mode == "NORMAL"
    assert kinds(log) == []
    st, events = decide(st, inp(820, mean_enter=80.0), CFG)
    assert st.mode == "PROTECT"
    assert events == [Event("fan_on", "enter")]


def test_no_data_at_all_is_a_fault():
    st, events = decide(ControlState(), inp(100, last_sample_ts=None), CFG)
    assert events == [Event("sensor_fault")]
    assert st.mode == "NORMAL"


# ---------- ghi đè thủ công ----------

def test_override_wins_and_expires():
    st = apply_override(ControlState(), False, 0, CFG.override_ttl_s)
    assert st.override_fan is False and st.override_until == 7200
    st, log = run(st, range(0, 7200, 20), danger_level=200.0)
    # máy trạng thái vẫn chạy: đã vào BẢO VỆ, cảnh báo khẩn cấp vẫn phát (một lần)
    assert st.mode == "PROTECT"
    assert kinds(log) == ["danger", "fan_on"]
    assert all(fan_on(s, t) is False for t, s, _ in log)
    st, events = decide(st, inp(7200, danger_level=200.0), CFG)
    assert events == [Event("override_expired")]
    assert st.override_fan is None and st.override_until is None
    assert fan_on(st, 7200) is True


def test_override_cleared_with_none():
    st = apply_override(PROTECT_SINCE_0, False, 0, 7200)
    assert fan_on(st, 10) is False
    st = apply_override(st, None, 10, 7200)
    assert st.override_fan is None and st.override_until is None
    assert fan_on(st, 10) is True


def test_override_works_during_sensor_gap():
    st, log = run(PROTECT_SINCE_0, range(20, 401, 20), last_sample_ts=0)
    assert st.fault_active is True
    st = apply_override(st, False, 400, 7200)
    st, log = run(st, range(420, 1001, 20), last_sample_ts=0)
    assert all(fan_on(s, t) is False for t, s, _ in log)
    assert st.mode == "PROTECT"
    st = apply_override(st, True, 1000, 7200)
    assert fan_on(st, 1000) is True
    # ghi đè hết hạn ngay cả khi đang mất dữ liệu
    st, events = decide(st, inp(8200, last_sample_ts=0), CFG)
    assert events == [Event("override_expired")]
    assert fan_on(st, 8200) is True     # quay về theo mode (vẫn BẢO VỆ)


# ---------- chống nhảy trạng thái ----------

def test_no_flapping_on_oscillation():
    pm = lambda t: 45.0 + 15.0 * math.sin(2 * math.pi * t / 240)     # 30..60, chu kỳ 4 phút
    st, log = simulate(pm, 1200, 1200 + 3600)
    assert transitions(log) <= 1


def test_no_flapping_square_wave_around_thresholds():
    pm = lambda t: 70.0 if (t // 120) % 2 == 0 else 20.0             # vuông 20/70, chu kỳ 4 phút
    st, log = simulate(pm, 1200, 1200 + 3600, st=PROTECT_SINCE_0)
    assert transitions(log, "PROTECT") <= 1


# ---------- lưu trạng thái ----------

def test_state_json_roundtrip():
    st = ControlState(mode="PROTECT", entered_at=100, enter_since=None, exit_since=200,
                      fault_active=True, override_fan=False, override_until=7300,
                      danger_active=True)
    assert ControlState.from_json(st.to_json()) == st
    assert ControlState.from_json(ControlState().to_json()) == ControlState()


@pytest.mark.parametrize("text", [
    "",
    "not json",
    "[]",
    "null",
    json.dumps({"mode": "NORMAL"}),                                    # thiếu trường
    json.dumps({**json.loads(ControlState().to_json()), "mode": "ON"}),
    json.dumps({**json.loads(ControlState().to_json()), "entered_at": "100"}),
    json.dumps({**json.loads(ControlState().to_json()), "fault_active": 1}),
    json.dumps({**json.loads(ControlState().to_json()), "override_fan": "off"}),
    json.dumps({**json.loads(ControlState().to_json()), "enter_since": True}),
])
def test_state_from_bad_json_raises(text):
    with pytest.raises(ValueError):
        ControlState.from_json(text)
