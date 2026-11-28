"""Mẫu 1 giây: SensorHub giữ giá trị mới nhất, collector ghi fast_readings và vẫn ghi raw 10 giây."""
from datetime import datetime, timedelta

import pytest

from aiot.collector import CsvLog, run
from aiot.db import connect, fast_rows, init_schema, insert_label, purge_fast
from aiot.pms7003 import PmsReading
from aiot.sensors import SensorHub


class Fake:
    def __init__(self):
        self.pm = PmsReading(10, 20, 25, 3000, 900, 200, 40, 6, 1)

    def read_pm(self):
        return self.pm

    def read_lux(self):
        return 150.0

    def read_dht(self):
        return (26.0, 75.0)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    yield c
    c.close()


def test_hub_snapshot_and_staleness():
    t = [0.0]
    hub = SensorHub(Fake(), clock=lambda: t[0])
    for key in ("pm", "lux", "dht"):
        hub.poll(key)
    snap = hub.snapshot()
    assert snap["pm25"] == 20.0 and snap["n03"] == 3000.0 and snap["hum"] == 75.0
    t[0] = 5.0                          # PM, lux cũ hơn 3 s; DHT còn mới (<= 10 s)
    snap = hub.snapshot()
    assert snap["pm25"] is None and snap["lux"] is None and snap["temp"] == 26.0
    assert hub.read_pm() is None


def test_hub_poll_error_keeps_last_value():
    class Bad(Fake):
        def read_pm(self):
            raise OSError("UART")
    t = [0.0]
    hub = SensorHub(Fake(), clock=lambda: t[0])
    hub.poll("pm")
    hub._sensors = Bad()
    hub.poll("pm")
    assert hub.read_pm().pm25 == 20


class Stop:
    def __init__(self, t, loops):
        self.t, self.left = t, loops

    def is_set(self):
        return self.left <= 0

    def wait(self, d):
        self.t[0] += d
        self.left -= 1


def test_run_fast_mode_writes_both_tables(conn, tmp_path, monkeypatch):
    import aiot.collector as col
    monkeypatch.setattr(col, "aggregate_pending", lambda c, now: None)
    t = [0.0]
    hub = SensorHub(Fake(), clock=lambda: t[0])
    base = datetime(2026, 11, 23, 21, 0, 0)
    for key in ("pm", "lux", "dht"):
        hub.poll(key)
    # giữ giá trị luôn mới
    hub._clock = lambda: 0.0
    run(hub, conn, CsvLog(str(tmp_path / "s.csv"), "pi01"), Stop(t, 25),
        clock=lambda: t[0], synced=lambda: True, wall=lambda: base + timedelta(seconds=t[0]))
    n_fast = conn.execute("SELECT COUNT(*) FROM fast_readings").fetchone()[0]
    n_raw = conn.execute("SELECT COUNT(*) FROM raw_readings").fetchone()[0]
    assert n_fast == 25
    assert n_raw == 3                    # t = 0, 10, 20
    rows = fast_rows(conn, 0)
    assert rows[0][1]["n100"] == 1.0 and rows[0][1]["lux"] == 150.0


def test_purge_fast_keeps_labelled_segment(conn):
    from aiot.db import insert_fast_many
    rows = [(ts, {"pm25": 10.0}, True) for ts in range(0, 20000, 100)]
    insert_fast_many(conn, rows)
    insert_label(conn, 10000, 10500, "pollution", "nhang")
    purge_fast(conn, 19000, margin_s=0)
    kept = [ts for ts, _ in fast_rows(conn, 0)]
    assert all(10000 <= ts <= 10500 or ts >= 19000 for ts in kept)
    assert 10200 in kept and 500 not in kept


def test_insert_label_validates(conn):
    with pytest.raises(ValueError):
        insert_label(conn, 10, 20, "dust")
    with pytest.raises(ValueError):
        insert_label(conn, 20, 10, "cooking")


def test_fast_rows_saturates_high_pm_and_drops_garbage(conn):
    """Khói nhang thật lên 1513 µg/m³: ghim về 1000 (bão hoà), không coi là thiếu mẫu.
    Khung hỏng (65535) và số âm vẫn là None."""
    from aiot.db import insert_fast_many
    insert_fast_many(conn, [(1, {"pm1": 440.0, "pm25": 1513.0, "pm10": 3141.0}, True),
                            (2, {"pm1": 65535.0, "pm25": -1.0, "pm10": 1000.0}, True)])
    (_, a), (_, b) = fast_rows(conn, 0)
    assert (a["pm1"], a["pm25"], a["pm10"]) == (440.0, 1000.0, 1000.0)
    assert (b["pm1"], b["pm25"], b["pm10"]) == (None, None, 1000.0)
