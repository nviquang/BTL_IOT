"""Test gộp trung bình 5 phút: lọc giá trị lỗi, MQ135, cờ valid, gộp bù idempotent."""
import pytest

from aiot.aggregate import aggregate_pending, aggregate_window
from aiot.db import RawSample, connect, init_schema, insert_raw


def sample(ts, pm25=10.0, clock_synced=True, **kw):
    vals = dict(pm1=5.0, pm10=15.0, temp=25.0, hum=60.0, lux=100.0, mq135_do=1)
    vals.update(kw)
    return RawSample(ts=ts, pm25=pm25, clock_synced=clock_synced, **vals)


def full_window(start, n=30, **kw):
    """n mẫu cách nhau 10 giây bắt đầu từ start."""
    return [sample(start + 10 * i, **kw) for i in range(n)]


def test_mean_of_window():
    samples = full_window(0, n=28) + [sample(280, pm25=20.0, temp=30.0), sample(290, pm25=40.0, temp=20.0)]
    r = aggregate_window(samples, 0)
    assert r["ts"] == 0
    assert r["pm25"] == pytest.approx((28 * 10 + 20 + 40) / 30)
    assert r["temp"] == pytest.approx(25.0)
    assert r["pm1"] == pytest.approx(5.0)
    assert r["pm10"] == pytest.approx(15.0)
    assert r["hum"] == pytest.approx(60.0)
    assert r["lux"] == pytest.approx(100.0)
    assert r["valid"] == 1


def test_out_of_range_dropped():
    samples = full_window(0, n=26) + [
        sample(260, pm25=1500.0),
        sample(270, pm25=-1.0, temp=60.0),
        sample(280, hum=120.0, temp=-5.0),
        sample(290, lux=70000.0, hum=-1.0),
    ]
    r = aggregate_window(samples, 0)
    assert r["pm25"] == pytest.approx(10.0)
    assert r["temp"] == pytest.approx(25.0)
    assert r["hum"] == pytest.approx(60.0)
    assert r["lux"] == pytest.approx(100.0)


def test_mq135_any_zero_gives_zero():
    samples = [sample(i * 10, mq135_do=v) for i, v in enumerate([1, 1, 0, 1])]
    assert aggregate_window(samples, 0)["mq135_do"] == 0
    samples = [sample(i * 10, mq135_do=v) for i, v in enumerate([1, None, 1])]
    assert aggregate_window(samples, 0)["mq135_do"] == 1
    samples = [sample(i * 10, mq135_do=None) for i in range(3)]
    assert aggregate_window(samples, 0)["mq135_do"] is None


def test_valid_needs_half_of_pm_samples():
    ok = full_window(0, n=15) + full_window(150, n=15, pm25=None)
    assert aggregate_window(ok, 0)["valid"] == 1
    bad = full_window(0, n=14) + full_window(140, n=16, pm25=None)
    assert aggregate_window(bad, 0)["valid"] == 0
    # PM ngoài khoảng không được tính là mẫu hợp lệ
    bad2 = full_window(0, n=14) + [sample(140, pm25=5000.0)]
    assert aggregate_window(bad2, 0)["valid"] == 0


def test_dht_failure_keeps_window_valid():
    r = aggregate_window(full_window(0, temp=None, hum=None), 0)
    assert r["temp"] is None
    assert r["hum"] is None
    assert r["valid"] == 1


def test_unsynced_clock_marks_invalid():
    samples = full_window(0)
    samples[5] = sample(50, clock_synced=False)
    assert aggregate_window(samples, 0)["valid"] == 0


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    return c


def readings_ts(conn):
    return [r[0] for r in conn.execute("SELECT ts FROM readings ORDER BY ts")]


def test_pending_is_idempotent(conn):
    # cửa sổ 0, 300 đầy đủ; cửa sổ 600 trống (không sinh dòng); 900 đang chạy
    for s in full_window(0) + full_window(300) + full_window(900, n=10):
        insert_raw(conn, s)
    aggregate_pending(conn, now=1000)
    assert readings_ts(conn) == [0, 300]
    aggregate_pending(conn, now=1000)
    assert readings_ts(conn) == [0, 300]  # không trùng, 900 chưa kết thúc

    # crash: collector ghi tiếp tới 1800 mà không gộp; sau đó gộp bù hết
    for s in full_window(1000, n=20) + full_window(1200) + full_window(1500):
        insert_raw(conn, s)
    aggregate_pending(conn, now=1805)
    assert readings_ts(conn) == [0, 300, 900, 1200, 1500]
    row = conn.execute("SELECT pm25, valid FROM readings WHERE ts = 900").fetchone()
    assert row == (pytest.approx(10.0), 1)


def test_pending_window_end_equal_now_is_closed(conn):
    for s in full_window(0):
        insert_raw(conn, s)
    aggregate_pending(conn, now=299)
    assert readings_ts(conn) == []
    aggregate_pending(conn, now=300)
    assert readings_ts(conn) == [0]


def test_error_mid_loop_rolls_back_and_releases_lock(tmp_path, monkeypatch):
    """Lỗi giữa vòng gộp không được để transaction mở (giữ khoá ghi WAL của collector)."""
    import aiot.aggregate as agg

    conn = connect(tmp_path / "a.db")
    init_schema(conn)
    for s in full_window(0) + full_window(300):
        insert_raw(conn, s)

    real = agg.aggregate_window
    calls = []

    def flaky(samples, start, window_s=300):
        calls.append(start)
        if len(calls) == 2:
            raise RuntimeError("lỗi giữa vòng")
        return real(samples, start, window_s)

    monkeypatch.setattr(agg, "aggregate_window", flaky)
    with pytest.raises(RuntimeError):
        aggregate_pending(conn, now=10_000)
    assert conn.in_transaction is False
    # Rollback cả cửa sổ đầu, lần chạy sau gộp lại từ đầu
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0
    monkeypatch.setattr(agg, "aggregate_window", real)
    aggregate_pending(conn, now=10_000)
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 2
    conn.close()
