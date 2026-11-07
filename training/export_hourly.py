"""
Xuất chuỗi trung bình giờ từ SQLite của trạm ra CSV để huấn luyện trên laptop.

- Chỉ dùng dòng readings valid=1 (TB 5 phút). Giờ có ít hơn 6/12 cửa sổ hợp lệ bị bỏ,
  cùng quy tắc với aiot/forecast.py (dùng lại HOUR_S, MIN_WINDOWS của module đó).
- Trong một giờ được giữ, cột nào có ít hơn 6 giá trị (cảm biến đó lỗi) thì để trống.
- Mốc giờ là đầu giờ theo giờ địa phương (mặc định Asia/Ho_Chi_Minh, UTC+7, lệch giờ
  nguyên nên trùng mốc epoch chia hết 3600), ghi kèm độ lệch, ví dụ 2026-11-05 07:00:00+07:00.

Chạy: python training/export_hourly.py --db aiot.db --out hourly.csv
DB trên Pi ở /home/admin/iot_data/aiot.db (db_path trong config.yaml). DB đang chạy ở chế
độ WAL nên không scp thẳng file (có thể thiếu trang còn nằm trong -wal); tạo bản sao nhất
quán trên Pi rồi mới chép về:
  ssh admin@pi.local 'sqlite3 ~/iot_data/aiot.db ".backup /tmp/aiot_copy.db"'
  scp admin@pi.local:/tmp/aiot_copy.db aiot.db
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

if __package__ in (None, ""):          # chạy trực tiếp: thêm thư mục gốc repo vào path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiot.forecast import HOUR_S, MIN_WINDOWS  # noqa: E402

COLUMNS = ["pm25", "pm10", "temp", "hum"]
DEFAULT_TZ = "Asia/Ho_Chi_Minh"


def hourly_frame(conn: sqlite3.Connection, tz: str = DEFAULT_TZ) -> pd.DataFrame:
    """DataFrame index là đầu giờ (tz địa phương), cột COLUMNS; chỉ giờ đủ MIN_WINDOWS."""
    agg = ", ".join(f"AVG({c}), COUNT({c})" for c in COLUMNS)
    rows = conn.execute(
        f"SELECT (ts / ?) * ?, COUNT(*), {agg} FROM readings "
        "WHERE valid = 1 GROUP BY ts / ? ORDER BY 1",
        (HOUR_S, HOUR_S, HOUR_S),
    ).fetchall()
    records, index = [], []
    for row in rows:
        hour, n = row[0], row[1]
        if n < MIN_WINDOWS:
            continue
        rec = {}
        for i, col in enumerate(COLUMNS):
            avg, cnt = row[2 + 2 * i], row[3 + 2 * i]
            rec[col] = float(avg) if cnt >= MIN_WINDOWS else float("nan")
        records.append(rec)
        index.append(int(hour))
    idx = pd.to_datetime(index, unit="s", utc=True).tz_convert(tz)
    return pd.DataFrame(records, index=idx, columns=COLUMNS).rename_axis("timestamp")


def export_hourly(db_path, out_path, tz: str = DEFAULT_TZ) -> int:
    """Ghi CSV timestamp,pm25,pm10,temp,hum; trả về số giờ đã ghi."""
    conn = sqlite3.connect(db_path)
    try:
        df = hourly_frame(conn, tz)
    finally:
        conn.close()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, float_format="%.3f")
    return len(df)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Xuất PM2.5/PM10/nhiệt độ/độ ẩm trung bình giờ")
    p.add_argument("--db", required=True, help="đường dẫn aiot.db")
    p.add_argument("--out", required=True, help="file CSV đầu ra")
    p.add_argument("--tz", default=DEFAULT_TZ, help=f"múi giờ (mặc định {DEFAULT_TZ})")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):        # console Windows mặc định cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if not Path(args.db).exists():
        print(f"không thấy DB: {args.db}", file=sys.stderr)
        return 1
    n = export_hourly(args.db, args.out, args.tz)
    print(f"đã ghi {n} giờ vào {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
