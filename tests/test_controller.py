"""Test dịch vụ controller: khôi phục sau khởi động lại, ghi đè, ghi sự kiện, lỗi relay."""
import importlib.util
import json
import sys
import threading
import types
from pathlib import Path

import pytest

from aiot.config import ControlCfg
from aiot.control import ControlState
from aiot.controller import PENDING_KEY, STATE_KEY, restore, run, step
from aiot.db import RawSample, connect, get_state, init_schema, insert_raw, pm25_series, set_state

CFG = ControlCfg()
ROOT = Path(__file__).resolve().parent.parent


class FakeRelay:
    """Ghi lại mọi lệnh set; fail = số lần set đầu tiên sẽ raise."""

    def __init__(self, fail=0):
        self.calls = []
        self.fail = fail
        self.closed = False

    def set(self, on):
        self.calls.append(on)
        if self.fail:
            self.fail -= 1
            raise OSError("gpio busy")

    def close(self):
        self.closed = True


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    yield c
    c.close()


def add_pm(conn, t_from, t_to, value, step_s=10):
    for t in range(t_from, t_to + 1, step_s):
        insert_raw(conn, RawSample(t, None, value, None, None, None, None, None, True))


def events(conn, kind=None):
    rows = conn.execute("SELECT ts, kind, detail FROM events ORDER BY rowid").fetchall()
    return [r for r in rows if kind is None or r[1] == kind]


def kinds(conn):
    return [k for _, k, _ in events(conn)]


def saved(conn):
    return ControlState.from_json(get_state(conn, STATE_KEY))


# ---------- khôi phục khi khởi động ----------

def test_first_boot_defaults_to_normal_fan_off(conn):
    relay = FakeRelay()
    st = restore(conn, relay, CFG, 1000)
    assert st == ControlState()
    assert relay.calls == [False]
    assert saved(conn) == ControlState()
    assert events(conn) == []


def test_restart_in_protect_turns_fan_on(conn):
    set_state(conn, STATE_KEY, ControlState(mode="PROTECT", entered_at=1000).to_json())
    relay = FakeRelay()
    st = restore(conn, relay, CFG, 2000)
    assert st.mode == "PROTECT"
    assert relay.calls == [True]


def test_restart_keeps_entered_at(conn):
    # min_state_s 1200 > exit_hold_s 900: nếu entered_at bị đặt lại lúc khởi động (1000)
    # thì phải tới 2200 mới tắt; giữ entered_at=0 thì tắt đúng 1000 + 900 = 1900.
    cfg = ControlCfg(min_state_s=1200)
    set_state(conn, STATE_KEY, ControlState(mode="PROTECT", entered_at=0).to_json())
    relay = FakeRelay()
    st = restore(conn, relay, cfg, 1000)
    assert st.entered_at == 0
    add_pm(conn, 0, 2500, 20.0)
    for now in range(1000, 2500, 20):
        st = step(conn, relay, cfg, now)
    off = events(conn, "fan_off")
    assert [ts for ts, _, _ in off] == [1900]
    assert st.mode == "NORMAL" and relay.calls[-1] is False


def test_restart_with_expired_override_returns_to_auto(conn):
    set_state(conn, STATE_KEY,
              ControlState(override_fan=True, override_until=1500).to_json())
    relay = FakeRelay()
    st = restore(conn, relay, CFG, 2000)
    assert st.override_fan is None and st.override_until is None
    assert relay.calls == [False]
    assert kinds(conn) == ["override_expired"]
    assert saved(conn).override_fan is None


def test_restart_with_live_override_keeps_it(conn):
    set_state(conn, STATE_KEY,
              ControlState(override_fan=True, override_until=5000).to_json())
    relay = FakeRelay()
    st = restore(conn, relay, CFG, 2000)
    assert st.override_fan is True
    assert relay.calls == [True]


def test_corrupt_state_falls_back_to_normal_and_logs(conn):
    set_state(conn, STATE_KEY, "{not json")
    relay = FakeRelay()
    st = restore(conn, relay, CFG, 1000)
    assert st == ControlState()
    assert relay.calls == [False]
    assert kinds(conn) == ["state_corrupt"]
    assert saved(conn) == ControlState()


# ---------- một bước điều khiển ----------

def test_transition_logged_to_events(conn):
    add_pm(conn, 900, 1000, 200.0)
    relay = FakeRelay()
    st = step(conn, relay, CFG, 1000)
    assert st.mode == "PROTECT"
    assert [(k, d) for _, k, d in events(conn)] == [("danger", ""), ("fan_on", "danger")]
    assert all(ts == 1000 for ts, _, _ in events(conn))
    assert relay.calls == [True]
    assert saved(conn) == st


def test_step_drives_relay_every_loop(conn):
    add_pm(conn, 0, 1200, 20.0)
    relay = FakeRelay()
    for now in (1000, 1020, 1040):
        step(conn, relay, CFG, now)
    assert relay.calls == [False, False, False]
    assert events(conn) == []


def test_stale_data_keeps_state_and_logs_fault(conn):
    set_state(conn, STATE_KEY, ControlState(mode="PROTECT", entered_at=0).to_json())
    add_pm(conn, 0, 500, 20.0)          # mẫu cuối ở 500, 1000 - 500 > stale_s
    relay = FakeRelay()
    st = step(conn, relay, CFG, 1000)
    st = step(conn, relay, CFG, 1020)
    assert st.mode == "PROTECT"
    assert relay.calls == [True, True]
    assert kinds(conn) == ["sensor_fault"]


def test_step_reads_enough_history(conn):
    # Cửa sổ dài nhất là exit_mean_s (900 s): chuỗi đọc ra phải phủ đủ để tính TB thoát
    set_state(conn, STATE_KEY, ControlState(mode="PROTECT", entered_at=0).to_json())
    add_pm(conn, 0, 3000, 20.0)
    relay = FakeRelay()
    st = step(conn, relay, CFG, 3000)
    assert st.exit_since == 3000


# ---------- ghi đè thủ công từ bot ----------

def test_pending_override_applied_then_cleared(conn):
    add_pm(conn, 0, 1200, 20.0)
    relay = FakeRelay()
    set_state(conn, PENDING_KEY, json.dumps({"fan": True, "ttl_s": 600}))
    st = step(conn, relay, CFG, 1000)
    assert st.override_fan is True and st.override_until == 1600
    assert relay.calls == [True]
    assert get_state(conn, PENDING_KEY) is None
    (ts, kind, detail), = events(conn)
    assert (ts, kind) == (1000, "override") and "on" in detail and "1600" in detail

    step(conn, relay, CFG, 1020)            # khoá đã xoá: không áp dụng lại
    assert len(events(conn, "override")) == 1

    set_state(conn, PENDING_KEY, json.dumps({"fan": None}))
    st = step(conn, relay, CFG, 1040)
    assert st.override_fan is None and st.override_until is None
    assert relay.calls == [True, True, False]
    assert get_state(conn, PENDING_KEY) is None
    assert "cleared" in events(conn, "override")[-1][2]


def test_pending_override_off_wins_over_protect(conn):
    add_pm(conn, 900, 1000, 200.0)
    relay = FakeRelay()
    set_state(conn, PENDING_KEY, json.dumps({"fan": False, "ttl_s": 7200}))
    st = step(conn, relay, CFG, 1000)
    assert st.mode == "PROTECT"                 # máy trạng thái vẫn chạy bên dưới
    assert relay.calls == [False]


def test_pending_override_without_ttl_uses_config_default(conn):
    add_pm(conn, 0, 1200, 20.0)
    set_state(conn, PENDING_KEY, json.dumps({"fan": True}))
    st = step(conn, FakeRelay(), CFG, 1000)
    assert st.override_until == 1000 + CFG.override_ttl_s


@pytest.mark.parametrize("text", [
    "garbage",
    "[]",
    json.dumps({"ttl_s": 600}),
    json.dumps({"fan": "on", "ttl_s": 600}),
    json.dumps({"fan": True, "ttl_s": "600"}),
    json.dumps({"fan": True, "ttl_s": 0}),
    json.dumps({"fan": True, "ttl_s": True}),
])
def test_malformed_pending_override_logged_cleared_ignored(conn, text):
    add_pm(conn, 0, 1200, 20.0)
    relay = FakeRelay()
    set_state(conn, PENDING_KEY, text)
    st = step(conn, relay, CFG, 1000)
    assert st.override_fan is None
    assert relay.calls == [False]
    assert get_state(conn, PENDING_KEY) is None
    assert kinds(conn) == ["override_invalid"]


# ---------- lỗi relay ----------

def test_relay_failure_does_not_persist_wrong_state(conn):
    add_pm(conn, 900, 1100, 200.0)
    relay = FakeRelay(fail=2)
    for now in (1000, 1020):
        st = step(conn, relay, CFG, now)
        assert saved(conn).mode == "PROTECT"    # trạng thái logic đúng dù relay lỗi
    assert kinds(conn).count("relay_error") == 1   # một lần cho cả chuỗi lỗi
    st = step(conn, relay, CFG, 1040)              # lần sau thử lại và thành công
    assert relay.calls == [True, True, True]
    assert kinds(conn).count("relay_recovered") == 1
    step(conn, relay, CFG, 1060)
    assert kinds(conn).count("relay_recovered") == 1


def test_relay_failure_on_restore_logged(conn):
    relay = FakeRelay(fail=1)
    restore(conn, relay, CFG, 1000)
    assert kinds(conn) == ["relay_error"]


# ---------- vòng lặp dịch vụ ----------

def test_run_steps_until_stopped(conn):
    add_pm(conn, 0, 1200, 20.0)
    relay = FakeRelay()
    stop = threading.Event()
    times = iter([1000, 1020, 1040])

    def wall():
        t = next(times)
        if t == 1040:
            stop.set()
        return t + 0.5                          # giờ thực có phần lẻ: vẫn lưu số nguyên

    clock = iter(range(0, 1000, 20)).__next__
    run(conn, relay, CFG, stop, wall=wall, clock=clock)
    assert relay.calls == [False, False, False]
    assert type(json.loads(get_state(conn, STATE_KEY))["enter_since"]) in (int, type(None))


def test_run_survives_step_error(conn, monkeypatch):
    relay = FakeRelay()
    stop = threading.Event()
    calls = []

    def boom(*a):
        calls.append(a)
        if len(calls) >= 2:
            stop.set()
        raise RuntimeError("bug")

    monkeypatch.setattr("aiot.controller.step", boom)
    run(conn, relay, CFG, stop, wall=lambda: 1000, clock=iter(range(0, 1000, 20)).__next__)
    assert len(calls) == 2


# ---------- Relay thật với lgpio giả ----------

class FakeLgpio(types.ModuleType):
    def __init__(self):
        super().__init__("lgpio")
        self.log = []

    def gpiochip_open(self, chip):
        self.log.append(("open", chip))
        return 7

    def gpio_claim_output(self, h, pin, level):
        self.log.append(("claim", h, pin, level))

    def gpio_write(self, h, pin, level):
        self.log.append(("write", h, pin, level))

    def gpiochip_close(self, h):
        self.log.append(("close", h))


@pytest.mark.parametrize("initial,level", [(True, 1), (False, 0)])
def test_relay_claims_with_initial_level_and_closes_low(monkeypatch, initial, level):
    fake = FakeLgpio()
    monkeypatch.setitem(sys.modules, "lgpio", fake)
    from aiot.relay import Relay
    r = Relay(pin=23, initial=initial)
    r.set(True)
    r.set(False)
    r.close()
    r.close()                                   # gọi lại không lỗi
    assert fake.log == [("open", 0), ("claim", 7, 23, level), ("write", 7, 23, 1),
                        ("write", 7, 23, 0), ("write", 7, 23, 0), ("close", 7)]


def test_relay_releases_chip_when_claim_fails(monkeypatch):
    fake = FakeLgpio()

    def fail(*a):
        raise RuntimeError("GPIO busy")

    fake.gpio_claim_output = fail
    monkeypatch.setitem(sys.modules, "lgpio", fake)
    from aiot.relay import Relay
    with pytest.raises(RuntimeError):
        Relay(pin=23)
    assert fake.log == [("open", 0), ("close", 7)]


# ---------- scripts/inject_pm.py ----------

def load_inject():
    spec = importlib.util.spec_from_file_location("inject_pm", ROOT / "scripts" / "inject_pm.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_inject_refuses_production_db(capsys):
    mod = load_inject()
    assert mod.main(["--db", "/home/admin/iot_data/aiot.db", "--profile", "high", "--count", "1"]) == 2
    assert "--i-know" in capsys.readouterr().err


def test_inject_writes_profile(tmp_path):
    mod = load_inject()
    db = tmp_path / "demo.db"
    t = iter(range(5000, 6000, 10))
    rc = mod.main(["--db", str(db), "--profile", "step", "--phase", "2", "--count", "6",
                   "--interval", "0"], wall=lambda: next(t), sleep=lambda s: None)
    assert rc == 0
    c = connect(db)
    assert pm25_series(c, 0) == [(5000, 20.0), (5010, 20.0), (5020, 80.0), (5030, 80.0),
                                 (5040, 20.0), (5050, 20.0)]
    c.close()


@pytest.mark.parametrize("profile,value", [("high", 80.0), ("low", 20.0), ("danger", 200.0)])
def test_inject_constant_profiles(profile, value):
    mod = load_inject()
    assert [mod.profile_value(profile, i, 30) for i in range(3)] == [value] * 3


# ---------- ghi nguyên tử (fix round 1) ----------

def test_pending_changed_between_read_and_delete_survives(conn, tmp_path, monkeypatch):
    import aiot.controller as controller
    add_pm(conn, 0, 1200, 20.0)
    first = json.dumps({"fan": True, "ttl_s": 600})
    second = json.dumps({"fan": False, "ttl_s": 600})
    set_state(conn, PENDING_KEY, first)
    real_parse = controller._parse_pending

    def parse_then_bot_writes(text, default_ttl_s):
        bot = connect(tmp_path / "aiot.db")         # bot ghi lệnh mới giữa lúc đọc và xoá
        set_state(bot, PENDING_KEY, second)
        bot.close()
        return real_parse(text, default_ttl_s)

    monkeypatch.setattr(controller, "_parse_pending", parse_then_bot_writes)
    relay = FakeRelay()
    st = step(conn, relay, CFG, 1000)
    assert st.override_fan is True
    assert get_state(conn, PENDING_KEY) == second   # lệnh mới không bị xoá mất

    monkeypatch.setattr(controller, "_parse_pending", real_parse)
    st = step(conn, relay, CFG, 1020)
    assert st.override_fan is False
    assert get_state(conn, PENDING_KEY) is None
    assert relay.calls == [True, False]
    assert len(events(conn, "override")) == 2


def test_step_failure_after_reading_override_keeps_it_pending(conn, monkeypatch):
    import sqlite3
    import aiot.controller as controller
    add_pm(conn, 0, 1200, 20.0)
    text = json.dumps({"fan": True, "ttl_s": 600})
    set_state(conn, PENDING_KEY, text)

    def locked(*a):
        raise sqlite3.OperationalError("database is locked")

    real_series = controller.pm25_series
    monkeypatch.setattr(controller, "pm25_series", locked)
    with pytest.raises(sqlite3.OperationalError):
        step(conn, FakeRelay(), CFG, 1000)
    assert get_state(conn, PENDING_KEY) == text
    assert events(conn) == []
    assert get_state(conn, STATE_KEY) is None

    monkeypatch.setattr(controller, "pm25_series", real_series)
    st = step(conn, FakeRelay(), CFG, 1020)
    assert st.override_fan is True and get_state(conn, PENDING_KEY) is None
    assert len(events(conn, "override")) == 1


def test_write_failure_mid_transaction_persists_nothing(conn, monkeypatch):
    # Lỗi khi đang ghi sự kiện thứ hai: trạng thái, sự kiện đầu và việc xoá lệnh đều huỷ
    import sqlite3
    import aiot.controller as controller
    add_pm(conn, 900, 1100, 200.0)                  # danger + fan_on
    text = json.dumps({"fan": False, "ttl_s": 600})
    set_state(conn, PENDING_KEY, text)
    real_log = controller.log_event
    calls = []

    def flaky(*a, **kw):
        calls.append(a)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real_log(*a, **kw)

    monkeypatch.setattr(controller, "log_event", flaky)
    relay = FakeRelay()
    with pytest.raises(sqlite3.OperationalError):
        step(conn, relay, CFG, 1000)
    assert relay.calls == []                        # chưa lưu được thì chưa đổi quạt
    assert get_state(conn, STATE_KEY) is None
    assert get_state(conn, PENDING_KEY) == text
    assert events(conn) == []

    monkeypatch.setattr(controller, "log_event", real_log)
    st = step(conn, relay, CFG, 1020)               # vòng sau quyết định lại, không mất sự kiện
    assert st.mode == "PROTECT" and st.override_fan is False
    assert kinds(conn) == ["override", "danger", "fan_on"]
    assert relay.calls == [False]
