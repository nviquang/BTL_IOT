"""
Gán nhãn một sự kiện thật cho bộ nhận diện nguồn bụi (bảng source_labels).

Nhãn: normal | cooking | pollution | smoking. Thời gian nhận 'now', 'HH:MM[:SS]' (hôm nay,
giờ Pi) hoặc 'YYYY-MM-DD HH:MM[:SS]'. Đoạn có nhãn được giữ lại khi xoá mẫu 1 giây cũ.

Ví dụ (trên Pi, trong ~/BTL_IOT):
  venv/bin/python scripts/label_event.py --label cooking --start 18:05 --end 18:25 --note "rán trứng"
  venv/bin/python scripts/label_event.py --label smoking --start now --duration 300 --note nhang
  venv/bin/python scripts/label_event.py --list
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiot.db import SOURCE_LABELS, connect, init_schema, insert_label  # noqa: E402


def parse_time(text: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    text = text.strip()
    if text == "now":
        return int(now)
    if text.isdigit():
        return int(text)
    today = datetime.fromtimestamp(now).date()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(text, fmt).time()
            return int(datetime.combine(today, t).timestamp())
        except ValueError:
            pass
    raise ValueError(f"không hiểu thời gian {text!r}")


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description="Gán nhãn sự kiện nguồn bụi")
    p.add_argument("--db", help="file DB (mặc định db_path trong config.yaml)")
    p.add_argument("--label", choices=SOURCE_LABELS)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--duration", type=int, help="giây, thay cho --end")
    p.add_argument("--note", default=None)
    p.add_argument("--list", action="store_true", help="in các nhãn đã có")
    a = p.parse_args(argv)

    db = a.db
    if db is None:
        from aiot.config import load_config
        db = load_config(root / "config.yaml", root / ".env").db_path
    conn = connect(db)
    init_schema(conn)
    if a.list:
        for r in conn.execute("SELECT id, start_ts, end_ts, label, note FROM source_labels ORDER BY start_ts"):
            s = datetime.fromtimestamp(r[1]).strftime("%Y-%m-%d %H:%M:%S")
            e = datetime.fromtimestamp(r[2]).strftime("%H:%M:%S")
            print(f"#{r[0]} {s} -> {e} {r[3]:9s} {r[4] or ''}")
        return 0
    if not a.label or not a.start or (a.end is None and a.duration is None):
        p.error("cần --label, --start và --end hoặc --duration")
    start = parse_time(a.start)
    end = start + a.duration if a.duration is not None else parse_time(a.end)
    try:
        new_id = insert_label(conn, start, end, a.label, a.note)
    except ValueError as e:
        p.error(str(e))
    print(f"Đã ghi nhãn #{new_id}: {a.label} {datetime.fromtimestamp(start)} -> "
          f"{datetime.fromtimestamp(end)} ({end - start} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
