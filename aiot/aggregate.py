"""
Gộp mẫu thô 10 giây thành trung bình theo cửa sổ (mặc định 5 phút).

- Cửa sổ căn theo bội số của window_s trong giây epoch; readings.ts là đầu cửa sổ.
- Giá trị ngoài khoảng đo bị loại trước khi lấy trung bình.
- valid = 1 khi có ít nhất một nửa số mẫu PM2.5 hợp lệ (so với window_s // 10 mẫu
  kỳ vọng) và mọi mẫu đều có đồng hồ đã đồng bộ.
- Mất nhiệt độ/độ ẩm/ánh sáng chỉ làm cột đó là NULL, không làm cửa sổ mất valid.
"""
import sqlite3

from aiot.db import PM_RANGE, RawSample

SAMPLE_PERIOD_S = 10

# Khoảng giá trị chấp nhận được (bao gồm hai đầu)
RANGES = {
    "pm1": PM_RANGE,
    "pm25": PM_RANGE,           # db.pm25_series (controller) lọc cùng khoảng này
    "pm10": PM_RANGE,
    "temp": (0.0, 50.0),       # dải đo của DHT11
    "hum": (0.0, 100.0),
    "lux": (0.0, 65535.0),     # BH1750
}


def _in_range(field: str, v) -> bool:
    lo, hi = RANGES[field]
    return v is not None and lo <= v <= hi


def aggregate_window(samples: list[RawSample], start_ts: int, window_s: int = 300) -> dict:
    """Tính một dòng readings từ các mẫu thô của cửa sổ bắt đầu tại start_ts."""
    row = {"ts": int(start_ts)}
    valid_counts = {}
    for f in RANGES:
        vals = [getattr(s, f) for s in samples if _in_range(f, getattr(s, f))]
        valid_counts[f] = len(vals)
        row[f] = sum(vals) / len(vals) if vals else None

    # MQ135: chỉ là 1 bit, 0 = vượt ngưỡng; có một lần vượt là cả cửa sổ vượt
    do_vals = [s.mq135_do for s in samples if s.mq135_do is not None]
    row["mq135_do"] = (0 if 0 in do_vals else 1) if do_vals else None

    expected = window_s // SAMPLE_PERIOD_S
    enough_pm = valid_counts["pm25"] * 2 >= expected
    clock_ok = all(s.clock_synced for s in samples)
    row["valid"] = 1 if (enough_pm and clock_ok) else 0
    return row


def _row_to_sample(r) -> RawSample:
    ts, pm1, pm25, pm10, temp, hum, lux, do, synced = r
    return RawSample(ts, pm1, pm25, pm10, temp, hum, lux, do, bool(synced))


def aggregate_pending(conn: sqlite3.Connection, now: int, window_s: int = 300) -> None:
    """Gộp mọi cửa sổ đã kết thúc (đầu + window_s <= now) chưa có trong readings.

    Bắt đầu từ mẫu thô đầu tiên sau cửa sổ readings mới nhất, nên gộp bù được
    các cửa sổ bị bỏ lỡ khi crash, và chạy lại không sinh dòng trùng.
    Cửa sổ không có mẫu thô nào thì không sinh dòng.
    """
    # Cả vòng là một transaction: lỗi giữa chừng thì rollback, không để khoá ghi
    # WAL bị giữ trên kết nối dùng lâu dài của collector.
    with conn:
        last = conn.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
        after = last + window_s if last is not None else None

        while True:
            if after is None:
                r = conn.execute("SELECT MIN(ts) FROM raw_readings").fetchone()
            else:
                r = conn.execute("SELECT MIN(ts) FROM raw_readings WHERE ts >= ?", (after,)).fetchone()
            first = r[0]
            if first is None:
                break
            start = first - first % window_s
            end = start + window_s
            if end > now:
                break  # cửa sổ chưa kết thúc
            rows = conn.execute(
                "SELECT ts, pm1, pm25, pm10, temp, hum, lux, mq135_do, clock_synced "
                "FROM raw_readings WHERE ts >= ? AND ts < ? ORDER BY ts",
                (start, end),
            ).fetchall()
            agg = aggregate_window([_row_to_sample(x) for x in rows], start, window_s)
            conn.execute(
                "INSERT OR IGNORE INTO readings "
                "(ts, pm1, pm25, pm10, temp, hum, lux, mq135_do, valid) "
                "VALUES (:ts, :pm1, :pm25, :pm10, :temp, :hum, :lux, :mq135_do, :valid)",
                agg,
            )
            after = end  # nhảy tới mẫu thô kế tiếp, bỏ qua các cửa sổ trống
