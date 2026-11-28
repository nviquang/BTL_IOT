"""
Lớp lưu trữ SQLite dùng chung cho mọi dịch vụ của trạm.

- Nhiều tiến trình (collector, controller, bot, forecast) mở cùng một file,
  nên bật WAL và busy_timeout để không bị 'database is locked'.
- Mỗi hàm ghi commit ngay trước khi trả về, vì mỗi dịch vụ là một tiến trình riêng.
  set_state/delete_state/log_event có commit=False để gộp nhiều lệnh ghi vào một
  transaction; người gọi tự conn.commit() (hoặc conn.rollback() khi lỗi).
- Chuỗi PM truyền giữa các module luôn là list[(ts giây epoch, µg/m³)], tăng dần theo ts.
"""
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_readings (      -- mẫu thô mỗi 10 giây
  ts INTEGER PRIMARY KEY,
  pm1 REAL, pm25 REAL, pm10 REAL,
  temp REAL, hum REAL, lux REAL,
  clock_synced INTEGER NOT NULL                -- 0 = đồng hồ chưa đồng bộ NTP
);
CREATE TABLE IF NOT EXISTS readings (          -- trung bình 5 phút, ts = đầu cửa sổ
  ts INTEGER PRIMARY KEY,
  pm1 REAL, pm25 REAL, pm10 REAL,
  temp REAL, hum REAL, lux REAL,
  valid INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS forecasts (
  made_at INTEGER, horizon_h INTEGER, pm25_pred REAL,
  model TEXT NOT NULL,                         -- 'seasonal_naive' hoặc 'persistence'
  PRIMARY KEY (made_at, horizon_h)
);
CREATE TABLE IF NOT EXISTS events (            -- 'fan_on', 'fan_off', 'override', 'sensor_fault'...
  ts INTEGER, kind TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (            -- tin Telegram chờ gửi (gửi bù khi có mạng)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at INTEGER NOT NULL, kind TEXT NOT NULL,
  text TEXT NOT NULL, sent_at INTEGER,
  photo_path TEXT                              -- ảnh JPEG gửi kèm (NULL: chỉ chữ)
);
CREATE TABLE IF NOT EXISTS fast_readings (     -- mẫu 1 giây cho bộ nhận diện nguồn bụi
  ts INTEGER PRIMARY KEY,
  pm1 REAL, pm25 REAL, pm10 REAL,
  n03 REAL, n05 REAL, n10 REAL, n25 REAL, n50 REAL, n100 REAL,   -- số hạt / 0,1 L
  temp REAL, hum REAL, lux REAL,
  clock_synced INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS source_labels (     -- nhãn sự kiện thật (scripts/label_event.py)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,
  label TEXT NOT NULL,                         -- normal | cooking | pollution | smoking
  note TEXT
);
CREATE TABLE IF NOT EXISTS source_predictions (  -- dự đoán nguồn bụi mỗi 5 giây
  ts INTEGER PRIMARY KEY,
  label TEXT,                                  -- nhãn sau làm mượt (NULL: không đủ dữ liệu)
  raw_label TEXT, proba TEXT,                  -- nhãn và xác suất của riêng lần này (JSON)
  model TEXT
);
CREATE TABLE IF NOT EXISTS tg_messages (       -- message_id Telegram (nhận và gửi) cho /clear
  chat_id INTEGER, message_id INTEGER, ts INTEGER,
  PRIMARY KEY (chat_id, message_id)
);
"""


SOURCE_LABELS = ("normal", "cooking", "pollution", "smoking")
FAST_COLUMNS = ("pm1", "pm25", "pm10", "n03", "n05", "n10", "n25", "n50", "n100",
                "temp", "hum", "lux")

# Khoảng đo hợp lệ của PMS7003 (µg/m³, gồm hai đầu); aggregate.RANGES dùng chung giá trị này
PM_RANGE = (0.0, 1000.0)


@dataclass
class RawSample:
    """Một lần đọc cảm biến; giá trị đo là None khi đọc lỗi."""
    ts: int
    pm1: float | None
    pm25: float | None
    pm10: float | None
    temp: float | None
    hum: float | None
    lux: float | None
    clock_synced: bool


def connect(path) -> sqlite3.Connection:
    """Mở DB (tạo thư mục cha nếu chưa có), bật WAL và busy_timeout 5 giây."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Tạo bảng nếu chưa có và thêm cột mới vào DB cũ; chạy nhiều lần không sao."""
    conn.executescript(SCHEMA)
    _add_column(conn, "outbox", "photo_path", "TEXT")
    # MQ135 đã bị loại khỏi hệ thống: xoá cột của DB tạo bởi phiên bản cũ
    _drop_column(conn, "raw_readings", "mq135_do")
    _drop_column(conn, "readings", "mq135_do")
    conn.commit()


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """ALTER TABLE ... ADD COLUMN nếu bảng (tạo bởi phiên bản cũ) chưa có cột.
    Hai dịch vụ cùng khởi động có thể cùng thêm: lỗi 'duplicate column' thì bỏ qua."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def _drop_column(conn: sqlite3.Connection, table: str, column: str) -> None:
    """ALTER TABLE ... DROP COLUMN nếu bảng còn cột cũ (SQLite >= 3.35).
    Hai dịch vụ cùng khởi động có thể cùng xoá: lỗi 'no such column' thì bỏ qua."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        return
    try:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    except sqlite3.OperationalError as e:
        if "no such column" not in str(e).lower():
            raise


def insert_raw(conn: sqlite3.Connection, s: RawSample) -> None:
    """Ghi mẫu thô; trùng ts thì bỏ qua, giữ bản ghi đầu."""
    conn.execute(
        "INSERT OR IGNORE INTO raw_readings "
        "(ts, pm1, pm25, pm10, temp, hum, lux, clock_synced) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (int(s.ts), s.pm1, s.pm25, s.pm10, s.temp, s.hum, s.lux,
         1 if s.clock_synced else 0),
    )
    conn.commit()


def pm25_series(conn: sqlite3.Connection, since_ts: int) -> list[tuple[int, float]]:
    """PM2.5 thô từ since_ts trở đi, bỏ NULL và giá trị ngoài PM_RANGE (cùng quy tắc
    với bước gộp 5 phút, để controller không quyết định theo giá trị lỗi).

    Giữ cả mẫu clock_synced=0: controller vẫn phải chạy khi Pi khởi động lại
    mà chưa có mạng để đồng bộ giờ.
    """
    rows = conn.execute(
        "SELECT ts, pm25 FROM raw_readings WHERE ts >= ? AND pm25 IS NOT NULL "
        "AND pm25 BETWEEN ? AND ? ORDER BY ts",
        (int(since_ts), *PM_RANGE),
    ).fetchall()
    return [(int(ts), float(v)) for ts, v in rows]


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn: sqlite3.Connection, key: str, value, *, commit: bool = True) -> None:
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    if commit:
        conn.commit()


def delete_state(conn: sqlite3.Connection, key: str, value: str | None = None,
                 *, commit: bool = True) -> None:
    """Xoá một khoá trạng thái; không có khoá thì thôi.

    value khác None: chỉ xoá khi giá trị hiện tại đúng bằng value (giá trị đã đọc),
    để không xoá mất giá trị tiến trình khác vừa ghi đè.
    """
    if value is None:
        conn.execute("DELETE FROM state WHERE key = ?", (key,))
    else:
        conn.execute("DELETE FROM state WHERE key = ? AND value = ?", (key, value))
    if commit:
        conn.commit()


def log_event(conn: sqlite3.Connection, ts: int, kind: str, detail: str,
              *, commit: bool = True) -> None:
    conn.execute("INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)", (int(ts), kind, detail))
    if commit:
        conn.commit()


def purge_raw(conn: sqlite3.Connection, older_than_ts: int) -> None:
    """Xoá mẫu thô có ts < older_than_ts (bảng readings giữ nguyên)."""
    conn.execute("DELETE FROM raw_readings WHERE ts < ?", (int(older_than_ts),))
    conn.commit()


def record_message(conn: sqlite3.Connection, chat_id: int, message_id: int, ts: int,
                   *, commit: bool = True) -> None:
    """Ghi message_id một tin Telegram (bot nhận hoặc gửi) để /clear xoá được; trùng thì bỏ qua."""
    conn.execute("INSERT OR IGNORE INTO tg_messages (chat_id, message_id, ts) VALUES (?, ?, ?)",
                 (int(chat_id), int(message_id), int(ts)))
    if commit:
        conn.commit()


def recent_message_ids(conn: sqlite3.Connection, chat_id: int, since_ts: int) -> list[int]:
    """message_id đã ghi của chat có ts >= since_ts."""
    rows = conn.execute("SELECT message_id FROM tg_messages WHERE chat_id = ? AND ts >= ?",
                        (int(chat_id), int(since_ts))).fetchall()
    return [int(r[0]) for r in rows]


def forget_messages(conn: sqlite3.Connection, chat_id: int, message_ids,
                    *, commit: bool = True) -> None:
    """Xoá các hàng tg_messages của đúng chat này có message_id trong message_ids."""
    conn.executemany("DELETE FROM tg_messages WHERE chat_id = ? AND message_id = ?",
                     [(int(chat_id), int(m)) for m in message_ids])
    if commit:
        conn.commit()


def purge_messages(conn: sqlite3.Connection, older_than_ts: int, *, commit: bool = True) -> None:
    """Xoá hàng tg_messages có ts < older_than_ts (Telegram không cho xoá tin đó nữa)."""
    conn.execute("DELETE FROM tg_messages WHERE ts < ?", (int(older_than_ts),))
    if commit:
        conn.commit()


# ---------------- mẫu 1 giây và nhãn nguồn bụi ----------------
def insert_fast_many(conn: sqlite3.Connection, rows) -> None:
    """Ghi nhiều mẫu 1 giây; rows: [(ts, {cột: giá trị}, clock_synced)]. Trùng ts thì bỏ qua."""
    cols = ", ".join(FAST_COLUMNS)
    marks = ", ".join("?" * (len(FAST_COLUMNS) + 2))
    conn.executemany(
        f"INSERT OR IGNORE INTO fast_readings (ts, {cols}, clock_synced) VALUES ({marks})",
        [(int(ts), *[vals.get(c) for c in FAST_COLUMNS], 1 if synced else 0)
         for ts, vals, synced in rows])
    conn.commit()


def fast_rows(conn: sqlite3.Connection, since_ts: int, until_ts: int | None = None):
    """[(ts, {cột: giá trị})] từ since_ts (tới until_ts nếu có), tăng dần theo ts.
    PM ngoài PM_RANGE coi như không có (None)."""
    cols = ", ".join(FAST_COLUMNS)
    sql = f"SELECT ts, {cols} FROM fast_readings WHERE ts >= ?"
    args = [int(since_ts)]
    if until_ts is not None:
        sql += " AND ts <= ?"
        args.append(int(until_ts))
    out = []
    for r in conn.execute(sql + " ORDER BY ts", args):
        vals = dict(zip(FAST_COLUMNS, r[1:]))
        for c in ("pm1", "pm25", "pm10"):
            v = vals[c]
            if v is not None and not (PM_RANGE[0] <= v <= PM_RANGE[1]):
                vals[c] = None
        out.append((int(r[0]), vals))
    return out


def purge_fast(conn: sqlite3.Connection, older_than_ts: int, margin_s: int = 1800) -> None:
    """Xoá mẫu 1 giây cũ, GIỮ các đoạn có nhãn (cộng thêm margin_s hai đầu) để huấn luyện."""
    conn.execute(
        "DELETE FROM fast_readings WHERE ts < ? AND NOT EXISTS ("
        "SELECT 1 FROM source_labels l WHERE fast_readings.ts BETWEEN l.start_ts - ? "
        "AND l.end_ts + ?)", (int(older_than_ts), int(margin_s), int(margin_s)))
    conn.execute("DELETE FROM source_predictions WHERE ts < ?", (int(older_than_ts),))
    conn.commit()


def insert_label(conn: sqlite3.Connection, start_ts: int, end_ts: int, label: str,
                 note: str | None = None) -> int:
    if label not in SOURCE_LABELS:
        raise ValueError(f"nhãn phải là một trong {', '.join(SOURCE_LABELS)}")
    if int(end_ts) <= int(start_ts):
        raise ValueError("end_ts phải sau start_ts")
    cur = conn.execute("INSERT INTO source_labels (start_ts, end_ts, label, note) VALUES (?, ?, ?, ?)",
                       (int(start_ts), int(end_ts), label, note))
    conn.commit()
    return int(cur.lastrowid)


def insert_prediction(conn: sqlite3.Connection, ts: int, label, raw_label, proba_json, model,
                      *, commit: bool = True) -> None:
    conn.execute("INSERT OR REPLACE INTO source_predictions (ts, label, raw_label, proba, model) "
                 "VALUES (?, ?, ?, ?, ?)", (int(ts), label, raw_label, proba_json, model))
    if commit:
        conn.commit()
