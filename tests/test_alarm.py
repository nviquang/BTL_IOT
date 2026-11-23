"""Test luật còi báo vượt ngưỡng (hàm thuần) và driver còi với GPIO giả."""
import time

import pytest

from aiot.alarm import BURST, SILENCE, AlarmState, alarm_reason, is_muted, plan
from aiot.buzzer import Buzzer
from aiot.config import BuzzerCfg

CFG = BuzzerCfg()   # alarm_pm25=150, repeat_s=300


# ---------- alarm_reason ----------

@pytest.mark.parametrize("level, expected", [
    (None, None),                   # mất dữ liệu PM: không kêu theo PM
    (149.9, None),
    (150.0, "pm25"),
    (300.0, "pm25"),
])
def test_alarm_reason(level, expected):
    assert alarm_reason(level, CFG) == expected


def test_alarm_reason_uses_configured_threshold():
    assert alarm_reason(60.0, BuzzerCfg(alarm_pm25=55.0)) == "pm25"


# ---------- plan ----------

def test_quiet_when_nothing_happens():
    assert plan(AlarmState(), None, 1000, CFG) == (AlarmState(), None)


def test_burst_when_threshold_first_crossed():
    st, action = plan(AlarmState(), "pm25", 1000, CFG)
    assert action == BURST
    assert st == AlarmState("pm25", 1000)


def test_no_repeat_before_repeat_s_then_repeat():
    st = AlarmState("pm25", 1000)
    assert plan(st, "pm25", 1000 + 299, CFG) == (st, None)
    st2, action = plan(st, "pm25", 1000 + 300, CFG)
    assert action == BURST and st2.last_burst_at == 1300


def test_silence_and_reset_when_back_below_threshold():
    st, action = plan(AlarmState("pm25", 1000), None, 1020, CFG)
    assert action == SILENCE
    assert st == AlarmState()
    # vượt lại ngay thì kêu lại ngay
    assert plan(st, "pm25", 1040, CFG)[1] == BURST


def test_muted_silences_but_keeps_tracking():
    st, action = plan(AlarmState(), "pm25", 1000, CFG, muted=True)
    assert action == SILENCE
    assert st == AlarmState("pm25", None)
    # hết tắt tiếng mà vẫn vượt thì kêu ngay
    assert plan(st, "pm25", 1020, CFG, muted=False)[1] == BURST


def test_mute_during_burst_silences():
    st, action = plan(AlarmState("pm25", 1000), "pm25", 1020, CFG, muted=True)
    assert action == SILENCE and st == AlarmState("pm25", 1000)


def test_state_json_roundtrip_and_corrupt_input():
    st = AlarmState("pm25", 123)
    assert AlarmState.from_json(st.to_json()) == st
    for bad in (None, "", "{", "[]", '{"reason": "gas", "last_burst_at": "a"}'):
        assert AlarmState.from_json(bad).reason is None


@pytest.mark.parametrize("value, now, expected", [
    (None, 100, False), ("200", 100, True), ("100", 100, False), ("0", 100, False),
    ("abc", 100, False),
])
def test_is_muted(value, now, expected):
    assert is_muted(value, now) is expected


# ---------- driver còi ----------

class FakeGpio:
    def __init__(self):
        self.levels = []        # mọi mức đã ghi, kể cả mức khi claim
        self.closed = False

    def gpiochip_open(self, chip):
        return 7

    def gpio_claim_output(self, handle, pin, level):
        self.levels.append(level)

    def gpio_write(self, handle, pin, level):
        self.levels.append(level)

    def gpiochip_close(self, handle):
        self.closed = True


def test_active_low_claims_silent_high_and_sounds_low():
    gpio = FakeGpio()
    b = Buzzer(pin=5, active_low=True, gpio=gpio)
    assert gpio.levels == [1]           # nhận chân ở mức im
    b.set(True)
    b.set(False)
    assert gpio.levels == [1, 0, 1]
    b.close()
    assert gpio.closed and gpio.levels[-1] == 1
    b.close()                           # gọi lần hai không lỗi


def test_active_high_levels():
    gpio = FakeGpio()
    b = Buzzer(active_low=False, gpio=gpio)
    b.set(True)
    assert gpio.levels == [0, 1]
    b.close()
    assert gpio.levels[-1] == 0


def test_burst_beeps_then_ends_silent_without_blocking():
    gpio = FakeGpio()
    b = Buzzer(gpio=gpio)
    start = time.monotonic()
    b.burst(0.01, 0.01, 0.1)
    assert time.monotonic() - start < 0.05      # không chặn vòng điều khiển
    for _ in range(100):
        if not b.busy():
            break
        time.sleep(0.01)
    assert not b.busy()
    assert 0 in gpio.levels                     # đã kêu (kích mức thấp)
    assert gpio.levels[-1] == 1                 # kết thúc ở mức im
    b.close()


def test_silence_stops_burst_immediately():
    gpio = FakeGpio()
    b = Buzzer(gpio=gpio)
    b.burst(0.05, 0.05, 30)
    time.sleep(0.02)
    b.silence()
    assert not b.busy()
    assert gpio.levels[-1] == 1
    b.close()
