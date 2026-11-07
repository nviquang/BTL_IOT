"""Test lớp lưu trữ SQLite: WAL, hai kết nối cùng ghi, trùng ts, state, event, purge."""
import sqlite3
import threading

from aiot.db import (
    RawSample,
    connect,
    delete_state,
    get_state,
    init_schema,
    insert_raw,
    log_event,
    pm25_series,
    purge_raw,
    set_state,
)


def sample(ts, pm25=10.0, clock_synced=True, **kw):
    """Mẫu thô mặc định hợp lệ; ghi đè từng trường qua kw."""
    vals = dict(pm1=5.0, pm10=15.0, temp=25.0, hum=60.0, lux=100.0, mq135_do=1)
    vals.update(kw)
    return RawSample(ts=ts, pm25=pm25, clock_synced=clock_synced, **vals)


def open_db(path):
    conn = connect(path)
    init_schema(conn)
    return conn


def test_wal_and_busy_timeout(tmp_path):
    conn = open_db(tmp_path / "sub" / "aiot.db")  # thư mục cha chưa có
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_init_schema_idempotent_and_tables(tmp_path):
    conn = open_db(tmp_path / "aiot.db")
    init_schema(conn)  # chạy lần hai không lỗi
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"readings", "forecasts", "events", "raw_readings", "state", "outbox"} <= names
    cols = {r[1] for r in conn.execute("PRAGMA table_info(forecasts)")}
    assert "model" in cols


def test_two_connections_write_without_locked_error(tmp_path):
    path = tmp_path / "aiot.db"
    a = open_db(path)
    b = open_db(path)
    for i in range(50):
        insert_raw(a, sample(1000 + 2 * i))
        set_state(b, "fan", "on" if i % 2 else "off")
        log_event(b, 1000 + 2 * i + 1, "test", str(i))
    c = connect(path)
    assert c.execute("SELECT COUNT(*) FROM raw_readings").fetchone()[0] == 50
    assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 50
    assert get_state(c, "fan") == "on"


def test_two_connections_write_concurrently(tmp_path):
    """Hai kết nối ghi đồng thời (luồng riêng) không gặp 'database is locked'."""
    path = tmp_path / "aiot.db"
    open_db(path).close()
    errors = []

    def writer(offset):
        try:
            conn = connect(path)
            for i in range(200):
                insert_raw(conn, sample(offset + i))
                log_event(conn, offset + i, "w", str(offset))
            conn.close()
        except Exception as e:  # noqa: BLE001 - gom lỗi để assert ở luồng chính
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(o,)) for o in (0, 10_000)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    c = connect(path)
    assert c.execute("SELECT COUNT(*) FROM raw_readings").fetchone()[0] == 400


def test_duplicate_ts_ignored(tmp_path):
    conn = open_db(tmp_path / "aiot.db")
    insert_raw(conn, sample(1000, pm25=11.0))
    insert_raw(conn, sample(1000, pm25=99.0))  # không raise
    rows = conn.execute("SELECT pm25 FROM raw_readings").fetchall()
    assert rows == [(11.0,)]


def test_raw_stores_none_and_clock_flag(tmp_path):
    conn = open_db(tmp_path / "aiot.db")
    insert_raw(conn, sample(1000, pm25=None, temp=None, mq135_do=None, clock_synced=False))
    row = conn.execute("SELECT pm25, temp, mq135_do, clock_synced FROM raw_readings").fetchone()
    assert row == (None, None, None, 0)


def test_pm25_series_includes_unsynced_skips_null(tmp_path):
    conn = open_db(tmp_path / "aiot.db")
    insert_raw(conn, sample(990, pm25=1.0))
    insert_raw(conn, sample(1010, pm25=3.0, clock_synced=False))
    insert_raw(conn, sample(1000, pm25=2.0))
    insert_raw(conn, sample(1020, pm25=None))
    assert pm25_series(conn, 1000) == [(1000, 2.0), (1010, 3.0)]


def test_state_default_and_upsert(tmp_path):
    conn = open_db(tmp_path / "aiot.db")
    assert get_state(conn, "mode") is None
    assert get_state(conn, "mode", "auto") == "auto"
    set_state(conn, "mode", "manual")
    set_state(conn, "mode", "auto")
    assert get_state(conn, "mode") == "auto"
    assert conn.execute("SELECT COUNT(*) FROM state").fetchone()[0] == 1


def test_delete_state(tmp_path):
    conn = open_db(tmp_path / "aiot.db")
    set_state(conn, "pending_override", '{"fan": null}')
    delete_state(conn, "pending_override")
    assert get_state(conn, "pending_override") is None
    delete_state(conn, "pending_override")          # xoá khoá không có: không lỗi
    other = sqlite3.connect(tmp_path / "aiot.db")   # đã commit
    assert other.execute("SELECT COUNT(*) FROM state").fetchone()[0] == 0


def test_log_event(tmp_path):
    conn = open_db(tmp_path / "aiot.db")
    log_event(conn, 1234, "fan_on", "pm25=60")
    other = sqlite3.connect(tmp_path / "aiot.db")  # đã commit, kết nối khác thấy
    assert other.execute("SELECT ts, kind, detail FROM events").fetchall() == [(1234, "fan_on", "pm25=60")]


def test_purge_keeps_recent(tmp_path):
    conn = open_db(tmp_path / "aiot.db")
    for ts in (100, 200, 300, 400):
        insert_raw(conn, sample(ts))
    purge_raw(conn, 300)
    ts_left = [r[0] for r in conn.execute("SELECT ts FROM raw_readings ORDER BY ts")]
    assert ts_left == [300, 400]


def test_writes_without_commit_roll_back_together(tmp_path):
    conn = open_db(tmp_path / "aiot.db")
    set_state(conn, "a", "1")
    set_state(conn, "a", "2", commit=False)
    log_event(conn, 10, "fan_on", "enter", commit=False)
    delete_state(conn, "a", commit=False)
    conn.rollback()
    assert get_state(conn, "a") == "1"
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_delete_state_only_if_value_matches(tmp_path):
    conn = open_db(tmp_path / "aiot.db")
    set_state(conn, "k", "new")
    delete_state(conn, "k", value="old")
    assert get_state(conn, "k") == "new"
    delete_state(conn, "k", value="new")
    assert get_state(conn, "k") is None


def test_pm25_series_drops_out_of_range_values(tmp_path):
    """Cùng khoảng hợp lệ 0–1000 với bước gộp 5 phút: controller không thấy giá trị lỗi."""
    conn = open_db(tmp_path / "aiot.db")
    insert_raw(conn, sample(1000, pm25=-1.0))
    insert_raw(conn, sample(1010, pm25=0.0))
    insert_raw(conn, sample(1020, pm25=1000.0))
    insert_raw(conn, sample(1030, pm25=1000.5))
    insert_raw(conn, sample(1040, pm25=65535.0))
    assert pm25_series(conn, 0) == [(1010, 0.0), (1020, 1000.0)]


def test_outbox_has_photo_path_column(tmp_path):
    conn = open_db(tmp_path / "aiot.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
    assert "photo_path" in cols


def test_init_schema_migrates_old_outbox(tmp_path):
    """DB tạo trước khi có photo_path: init_schema thêm cột, giữ tin cũ, chạy lại không lỗi."""
    path = tmp_path / "aiot.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "created_at INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, "
                "sent_at INTEGER)")
    old.execute("INSERT INTO outbox (created_at, kind, text) VALUES (1, 'reply', 'cũ')")
    old.commit()
    old.close()
    conn = open_db(path)
    init_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
    assert "photo_path" in cols
    assert conn.execute("SELECT text, photo_path FROM outbox").fetchall() == [("cũ", None)]
