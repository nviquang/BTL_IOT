"""Test collector với cảm biến giả: SQLite, CSV đúng định dạng cũ, MQTT không bắt buộc."""
import json
import sqlite3
from datetime import datetime

import pytest

from aiot.collector import CsvLog, Warmup, collect_once, is_clock_trusted
from aiot.db import connect, init_schema
from aiot.payload import valid_sensor_row
from aiot.pms7003 import PmsReading

NOW = datetime(2026, 11, 5, 8, 0, 0)
HEADER = b"timestamp,device_id,temperature_c,humidity_pct,lux\r\n"


class FakeSensors:
    def __init__(self, dht=(28.5, 83.0), lux=170.8, pm=PmsReading(12, 20, 25), mq135=1):
        self.dht, self.lux, self.pm, self.mq135 = dht, lux, pm, mq135
        self.mq135_calls = 0

    def read_dht(self):
        return self.dht

    def read_lux(self):
        return self.lux

    def read_pm(self):
        return self.pm

    def read_mq135(self):
        self.mq135_calls += 1
        return self.mq135


class RaisingSensors(FakeSensors):
    """Mọi lần đọc đều raise: collector không được chết."""
    def read_dht(self):
        raise RuntimeError("DHT hỏng")

    def read_lux(self):
        raise OSError("I2C hỏng")

    def read_pm(self):
        raise OSError("UART hỏng")

    def read_mq135(self):
        raise OSError("lgpio hỏng")


class RecordingPublisher:
    def __init__(self):
        self.payloads = []

    def publish(self, payload):
        self.payloads.append(payload)


class BrokenPublisher:
    def publish(self, payload):
        raise ConnectionError("broker down")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    yield c
    c.close()


@pytest.fixture
def csv_path(tmp_path):
    return tmp_path / "data" / "sensors.csv"


def raw_rows(conn):
    return conn.execute(
        "SELECT ts, pm1, pm25, pm10, temp, hum, lux, mq135_do, clock_synced FROM raw_readings"
    ).fetchall()


def run(sensors, conn, csv_path, publisher=None, now=NOW, clock_synced=True, **kw):
    return collect_once(sensors, conn, CsvLog(str(csv_path), "pi01"), publisher or RecordingPublisher(),
                        now, clock_synced, **kw)


def test_dht_failure_still_stores_pm(conn, csv_path):
    s = run(FakeSensors(dht=None), conn, csv_path)
    assert s.pm25 == 20.0 and s.temp is None and s.hum is None
    rows = raw_rows(conn)
    assert rows == [(int(NOW.timestamp()), 12.0, 20.0, 25.0, None, None, 170.8, 1, 1)]


def test_csv_skipped_when_dht_or_lux_missing(conn, csv_path):
    run(FakeSensors(dht=None), conn, csv_path)
    run(FakeSensors(lux=None), conn, csv_path, now=datetime(2026, 11, 5, 8, 0, 10))
    # Không có dòng nào đủ dữ liệu: không tạo file, không có dòng rỗng
    assert not csv_path.exists() or csv_path.read_bytes() in (b"", HEADER)
    run(FakeSensors(), conn, csv_path, now=datetime(2026, 11, 5, 8, 0, 20))
    lines = csv_path.read_bytes().split(b"\r\n")
    assert lines[-1] == b""
    assert all(line for line in lines[:-1])
    assert len(lines) == 3  # header + 1 dòng + phần rỗng sau \r\n cuối


def test_csv_format_unchanged(conn, csv_path):
    run(FakeSensors(dht=(28.5, 83), lux=170.83), conn, csv_path)
    # Khởi động lại dịch vụ: CsvLog mới trên cùng file, header không ghi lại
    collect_once(FakeSensors(dht=(28.46, 83.04), lux=170.8), conn, CsvLog(str(csv_path), "pi01"),
                 RecordingPublisher(), datetime(2026, 11, 5, 8, 0, 10), True)
    assert csv_path.read_bytes() == (
        HEADER
        + b"2026-11-05T08:00:00,pi01,28.5,83.0,170.8\r\n"
        + b"2026-11-05T08:00:10,pi01,28.5,83.0,170.8\r\n"
    )


def test_mqtt_payload_accepted_by_server(conn, csv_path):
    pub = RecordingPublisher()
    run(FakeSensors(), conn, csv_path, publisher=pub)
    assert len(pub.payloads) == 1
    p = pub.payloads[0]
    assert valid_sensor_row(p)
    assert p["pm25"] == 20.0 and p["pm1"] == 12.0 and p["pm10"] == 25.0 and p["mq135_do"] == 1
    assert p["timestamp"] == "2026-11-05T08:00:00" and p["device_id"] == "pi01"
    json.dumps(p)  # phải gửi được dạng JSON


def test_mqtt_payload_allows_missing_pm(conn, csv_path):
    pub = RecordingPublisher()
    run(FakeSensors(pm=None, mq135=None), conn, csv_path, publisher=pub)
    p = pub.payloads[0]
    assert valid_sensor_row(p)
    assert p["pm25"] is None and p["mq135_do"] is None


def test_no_publish_when_csv_fields_missing(conn, csv_path):
    pub = RecordingPublisher()
    run(FakeSensors(lux=None), conn, csv_path, publisher=pub)
    assert pub.payloads == []


def test_broker_down_does_not_stop_collection(conn, csv_path):
    run(FakeSensors(), conn, csv_path, publisher=BrokenPublisher())
    assert len(raw_rows(conn)) == 1
    assert csv_path.read_bytes().count(b"\r\n") == 2


def test_all_sensors_fail_writes_nothing_and_survives(conn, csv_path):
    pub = RecordingPublisher()
    s = run(RaisingSensors(), conn, csv_path, publisher=pub)
    assert (s.pm25, s.temp, s.lux, s.mq135_do) == (None, None, None, None)
    s = run(FakeSensors(dht=None, lux=None, pm=None, mq135=None), conn, csv_path, publisher=pub,
            now=datetime(2026, 11, 5, 8, 0, 10))
    assert raw_rows(conn) == []
    assert not csv_path.exists() or csv_path.read_bytes() in (b"", HEADER)
    assert pub.payloads == []


def test_db_error_does_not_stop_csv(conn, csv_path):
    conn.close()  # mọi lệnh SQL sau đó đều lỗi
    run(FakeSensors(), conn, csv_path)
    assert csv_path.read_bytes().count(b"\r\n") == 2


def test_clock_unsynced_flag_stored(conn, csv_path):
    run(FakeSensors(), conn, csv_path, clock_synced=False)
    rows = raw_rows(conn)
    assert len(rows) == 1 and rows[0][-1] == 0


def test_mq135_ignored_during_warmup(conn, csv_path):
    t = [1000.0]
    warm = Warmup(180, clock=lambda: t[0])
    sensors = FakeSensors(mq135=0)

    s = run(sensors, conn, csv_path, mq135_ready=warm.ready())
    assert s.mq135_do is None and sensors.mq135_calls == 0
    t[0] += 179.9
    s = run(sensors, conn, csv_path, now=datetime(2026, 11, 5, 8, 0, 10), mq135_ready=warm.ready())
    assert s.mq135_do is None
    t[0] += 0.1
    s = run(sensors, conn, csv_path, now=datetime(2026, 11, 5, 8, 0, 20), mq135_ready=warm.ready())
    assert s.mq135_do == 0
    assert [r[7] for r in raw_rows(conn)] == [None, None, 0]


def test_is_clock_trusted(monkeypatch, tmp_path):
    no_flag = str(tmp_path / "khong_co")   # không có file cờ timesyncd: hỏi timedatectl
    import subprocess

    class Done:
        def __init__(self, out):
            self.stdout = out

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done("yes\n"))
    assert is_clock_trusted(flag_path=no_flag) is True
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done("no\n"))
    assert is_clock_trusted(flag_path=no_flag) is False

    def missing(*a, **k):
        raise FileNotFoundError("timedatectl")  # laptop Windows

    monkeypatch.setattr(subprocess, "run", missing)
    assert is_clock_trusted(flag_path=no_flag) is False


# ---------------- PiSensors: lỗi mở/đọc cổng không làm chết vòng lặp ----------------
def test_pms_open_failure_retried_later(monkeypatch):
    import sys
    import types

    from aiot import sensors as sensors_mod

    t = [0.0]
    opened = []

    class FakeSerial:
        def __init__(self, port, baud, timeout):
            opened.append(port)
            if len(opened) == 1:
                raise OSError("không có /dev/serial0")
            self.timeout = timeout

        def reset_input_buffer(self):
            raise OSError("cổng bị rút")  # lỗi khi đọc

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=FakeSerial))
    s = sensors_mod.PiSensors(clock=lambda: t[0], sleep=lambda _: None)
    assert s.read_pm() is None          # mở lỗi
    assert s.read_pm() is None and len(opened) == 1   # chưa tới hạn thử lại
    t[0] += sensors_mod.REOPEN_S
    assert s.read_pm() is None and len(opened) == 2   # mở được nhưng đọc lỗi -> None
    s.close()


def test_missing_hardware_libs_return_none():
    """Laptop không có board/lgpio: mọi hàm đọc trả None, không raise."""
    from aiot.sensors import PiSensors

    s = PiSensors(pms_port="COM_KHONG_CO", sleep=lambda _: None)
    assert s.read_dht() is None
    assert s.read_lux() is None
    assert s.read_mq135() is None
    s.close()


# ---------------- Vòng lặp: chỉ gộp khi đồng hồ đã đồng bộ ----------------
class FakeStop:
    """stop.wait() tăng đồng hồ giả thay vì ngủ; dừng sau max_loops vòng."""
    def __init__(self, t, max_loops):
        self.t, self.left = t, max_loops

    def is_set(self):
        return self.left <= 0

    def wait(self, delay):
        self.t[0] += delay
        self.left -= 1


@pytest.mark.parametrize("synced, expect_rows", [(True, True), (False, False)])
def test_run_aggregates_only_when_clock_synced(conn, csv_path, monkeypatch, synced, expect_rows):
    import aiot.collector as col

    calls = []
    monkeypatch.setattr(col, "aggregate_pending", lambda c, now: calls.append(now))
    monkeypatch.setattr(col, "purge_raw", lambda c, ts: calls.append(("purge", ts)))
    t = [0.0]
    col.run(FakeSensors(), conn, CsvLog(str(csv_path), "pi01"), RecordingPublisher(),
            FakeStop(t, max_loops=13), clock=lambda: t[0], synced=lambda: synced)
    assert bool(calls) is expect_rows
    if synced:
        assert sum(1 for c in calls if isinstance(c, tuple)) == 1   # purge đúng một lần


# ---------------- Vòng sửa lỗi sau review ----------------
def test_bh1750_reopen_does_not_leak_i2c_bus(monkeypatch):
    """BH1750 vắng mặt: mỗi lần thử mở lại phải giải phóng bus I2C vừa tạo."""
    import sys
    import types

    from aiot import sensors as sensors_mod

    created, deinited = [], []

    class FakeI2C:
        def __init__(self, scl, sda):
            created.append(self)

        def deinit(self):
            deinited.append(self)

    def no_sensor(i2c, address):
        raise ValueError("No I2C device at address: 0x23")

    monkeypatch.setitem(sys.modules, "board", types.SimpleNamespace(SCL=3, SDA=2))
    monkeypatch.setitem(sys.modules, "busio", types.SimpleNamespace(I2C=FakeI2C))
    monkeypatch.setitem(sys.modules, "adafruit_bh1750", types.SimpleNamespace(BH1750=no_sensor))
    t = [0.0]
    s = sensors_mod.PiSensors(clock=lambda: t[0], sleep=lambda _: None)
    for _ in range(5):
        assert s.read_lux() is None
        t[0] += sensors_mod.REOPEN_S
    assert len(created) == 5
    assert deinited == created
    s.close()


def test_bh1750_bus_released_on_close(monkeypatch):
    import sys
    import types

    from aiot import sensors as sensors_mod

    deinited = []

    class FakeI2C:
        def __init__(self, scl, sda):
            pass

        def deinit(self):
            deinited.append(self)

    class FakeBH1750:
        def __init__(self, i2c, address):
            self.lux = 170.8

    monkeypatch.setitem(sys.modules, "board", types.SimpleNamespace(SCL=3, SDA=2))
    monkeypatch.setitem(sys.modules, "busio", types.SimpleNamespace(I2C=FakeI2C))
    monkeypatch.setitem(sys.modules, "adafruit_bh1750", types.SimpleNamespace(BH1750=FakeBH1750))
    s = sensors_mod.PiSensors(sleep=lambda _: None)
    assert s.read_lux() == 170.8
    s.close()
    assert len(deinited) == 1


class CommitFailsConn:
    """Bọc kết nối thật; commit() lỗi như khi đầy đĩa hay khoá quá busy_timeout."""
    def __init__(self, real):
        self.real = real

    def execute(self, *a):
        return self.real.execute(*a)

    def commit(self):
        raise sqlite3.OperationalError("database or disk is full")

    def rollback(self):
        self.real.rollback()

    @property
    def in_transaction(self):
        return self.real.in_transaction


def test_failed_commit_rolls_back_and_releases_lock(conn, csv_path):
    run(FakeSensors(), CommitFailsConn(conn), csv_path)
    assert conn.in_transaction is False
    assert raw_rows(conn) == []
    assert csv_path.read_bytes().count(b"\r\n") == 2   # CSV vẫn được ghi


def test_failed_purge_rolls_back(conn, csv_path, monkeypatch):
    import aiot.collector as col

    monkeypatch.setattr(col, "aggregate_pending", lambda c, now: None)
    insert_conn = CommitFailsConn(conn)
    t = [0.0]
    col.run(FakeSensors(), insert_conn, CsvLog(str(csv_path), "pi01"), RecordingPublisher(),
            FakeStop(t, max_loops=7), clock=lambda: t[0], synced=lambda: True)
    assert conn.in_transaction is False


# ---------------- Final review fixes ----------------
class _Done:
    def __init__(self, out):
        self.stdout = out


def test_clock_trusted_once_synced_since_boot(monkeypatch, tmp_path):
    """File cờ của systemd-timesyncd còn đó (đã đồng bộ ít nhất một lần từ lúc khởi động):
    tin đồng hồ kể cả khi timedatectl báo NTPSynchronized=no sau nhiều giờ mất mạng."""
    import subprocess

    import aiot.collector as col

    flag = tmp_path / "synchronized"
    flag.write_text("")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Done("no\n"))
    assert col.is_clock_trusted(flag_path=str(flag)) is True


def test_clock_trusted_falls_back_to_timedatectl(monkeypatch, tmp_path):
    import subprocess

    import aiot.collector as col

    missing = str(tmp_path / "khong_co")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Done("yes\n"))
    assert col.is_clock_trusted(flag_path=missing) is True
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Done("no\n"))
    assert col.is_clock_trusted(flag_path=missing) is False


def test_clock_trusted_default_flag_path():
    import aiot.collector as col

    assert col.SYNC_FLAG_PATH == "/run/systemd/timesync/synchronized"


def test_untrusted_clock_skips_csv_and_mqtt(conn, csv_path):
    pub = RecordingPublisher()
    run(FakeSensors(), conn, csv_path, publisher=pub, clock_synced=False)
    assert len(raw_rows(conn)) == 1 and raw_rows(conn)[0][-1] == 0   # SQLite vẫn ghi
    assert not csv_path.exists()
    assert pub.payloads == []


def test_untrusted_sample_colliding_with_existing_ts_warns(conn, csv_path, caplog):
    import logging

    run(FakeSensors(), conn, csv_path, now=datetime(2026, 11, 5, 8, 0, 30))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="aiot.collector"):
        run(FakeSensors(), conn, csv_path, now=datetime(2026, 11, 5, 8, 0, 10), clock_synced=False)
    assert any("raw_readings" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="aiot.collector"):
        run(FakeSensors(), conn, csv_path, now=datetime(2026, 11, 5, 8, 1, 0), clock_synced=False)
    assert not any("raw_readings" in r.getMessage() for r in caplog.records)


def test_signal_handlers_installed_after_hardware_libs(monkeypatch):
    """Blinka (adafruit_dht -> pulseio) tự đăng ký SIGTERM gây SystemExit(1) khi được import.
    Collector phải import thư viện đó trước rồi mới đặt handler của mình, để SIGTERM dừng sạch."""
    import signal as _signal
    import types

    from aiot import collector

    def blinka_like_handler(signum, frame):
        raise SystemExit(1)

    def fake_import(name):
        _signal.signal(_signal.SIGTERM, blinka_like_handler)
        return types.ModuleType(name)

    old = _signal.getsignal(_signal.SIGTERM)
    try:
        stop = collector.threading.Event()
        collector.install_signal_handlers(stop, importer=fake_import)
        handler = _signal.getsignal(_signal.SIGTERM)
        assert handler is not blinka_like_handler
        handler(_signal.SIGTERM, None)          # không raise SystemExit
        assert stop.is_set()
    finally:
        _signal.signal(_signal.SIGTERM, old)


def test_signal_handlers_survive_missing_hardware_libs():
    import signal as _signal

    from aiot import collector

    def missing(name):
        raise ImportError(name)

    old = _signal.getsignal(_signal.SIGTERM)
    try:
        stop = collector.threading.Event()
        collector.install_signal_handlers(stop, importer=missing)
        _signal.getsignal(_signal.SIGTERM)(_signal.SIGTERM, None)
        assert stop.is_set()
    finally:
        _signal.signal(_signal.SIGTERM, old)


# ---------------- Trạng thái quạt gửi kèm payload ----------------
def _set_controller(conn, *, mode="NORMAL", override_fan=None, override_until=None,
                    heartbeat=None, relay_fault=False):
    from aiot.control import ControlState
    from aiot.db import set_state
    st = ControlState(mode=mode, override_fan=override_fan, override_until=override_until)
    set_state(conn, "control_state", st.to_json())
    set_state(conn, "controller_heartbeat", heartbeat if heartbeat is not None else int(NOW.timestamp()))
    if relay_fault:
        set_state(conn, "relay_fault", "1")


def test_payload_fan_unknown_without_controller(conn, csv_path):
    pub = RecordingPublisher()
    run(FakeSensors(), conn, csv_path, publisher=pub)
    p = pub.payloads[0]
    assert p["fan_on"] is None and p["fan_mode"] is None and p["fan_state"] is None


def test_payload_fan_follows_controller_state(conn, csv_path):
    _set_controller(conn, mode="PROTECT")
    pub = RecordingPublisher()
    run(FakeSensors(), conn, csv_path, publisher=pub)
    p = pub.payloads[0]
    assert p["fan_on"] is True and p["fan_mode"] == "auto" and p["fan_state"] == "PROTECT"
    assert p["fan_relay_fault"] is False
    json.dumps(p)


def test_payload_fan_manual_override(conn, csv_path):
    now = int(NOW.timestamp())
    _set_controller(conn, mode="NORMAL", override_fan=True, override_until=now + 600, relay_fault=True)
    pub = RecordingPublisher()
    run(FakeSensors(), conn, csv_path, publisher=pub)
    p = pub.payloads[0]
    assert p["fan_on"] is True and p["fan_mode"] == "manual" and p["fan_relay_fault"] is True


def test_payload_fan_unknown_when_heartbeat_stale(conn, csv_path):
    _set_controller(conn, mode="PROTECT", heartbeat=int(NOW.timestamp()) - 600)
    pub = RecordingPublisher()
    run(FakeSensors(), conn, csv_path, publisher=pub)
    assert pub.payloads[0]["fan_on"] is None


def test_fan_state_keys_match_controller():
    from aiot import collector, controller
    assert collector.CONTROL_STATE_KEY == controller.STATE_KEY
    assert collector.HEARTBEAT_KEY == controller.HEARTBEAT_KEY
    assert collector.RELAY_FAULT_KEY == controller.RELAY_FAULT_KEY
