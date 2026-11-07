"""
Ghi chuỗi PM2.5 giả vào một DB THỬ để trình diễn bộ điều khiển quạt (control.demo: true).

Mỗi --interval giây ghi một mẫu raw_readings với ts = giờ hiện tại, nên controller
đang chạy với cùng db_path sẽ phản ứng theo thời gian thực.

Hồ sơ:
  high   80 µg/m³ liên tục   (vượt ngưỡng vào 55)
  low    20 µg/m³ liên tục   (dưới ngưỡng thoát 35)
  danger 200 µg/m³ liên tục  (mức khẩn cấp >= 150)
  step   20 -> 80 -> 20, mỗi pha --phase mẫu

Ví dụ:
  python scripts/inject_pm.py --db ~/iot_data/demo.db --profile step --phase 12
Từ chối ghi vào DB thật /home/admin/iot_data/aiot.db trừ khi có --i-know.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiot.db import RawSample, connect, init_schema, insert_raw  # noqa: E402

PRODUCTION_DB = "/home/admin/iot_data/aiot.db"
CONSTANT = {"high": 80.0, "low": 20.0, "danger": 200.0}
PROFILES = (*CONSTANT, "step")


def profile_value(profile: str, i: int, phase: int) -> float:
    """Giá trị PM2.5 của mẫu thứ i (đếm từ 0)."""
    if profile in CONSTANT:
        return CONSTANT[profile]
    # step: pha 0 thấp, pha 1 cao, từ pha 2 trở đi thấp
    return 80.0 if i // phase == 1 else 20.0


def is_production(path: str) -> bool:
    p = os.path.normpath(os.path.expanduser(path)).replace("\\", "/")
    try:
        same = Path(path).expanduser().resolve() == Path(PRODUCTION_DB).resolve()
    except OSError:
        same = False
    return p == PRODUCTION_DB or same


def main(argv=None, *, wall=time.time, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(description="Ghi chuỗi PM2.5 giả vào DB thử")
    parser.add_argument("--db", required=True, help="đường dẫn DB thử (không dùng DB thật)")
    parser.add_argument("--profile", choices=PROFILES, default="step")
    parser.add_argument("--interval", type=float, default=10.0, help="giây giữa hai mẫu")
    parser.add_argument("--phase", type=int, default=12, help="số mẫu mỗi pha của hồ sơ step")
    parser.add_argument("--count", type=int, default=None,
                        help="số mẫu rồi dừng (mặc định: step chạy 3 pha, còn lại chạy tới Ctrl+C)")
    parser.add_argument("--i-know", action="store_true", help="cho phép ghi vào DB thật")
    args = parser.parse_args(argv)

    if is_production(args.db) and not args.i_know:
        print(f"Từ chối ghi vào DB thật {PRODUCTION_DB}; dùng DB thử hoặc thêm --i-know",
              file=sys.stderr)
        return 2
    if args.phase <= 0:
        print("--phase phải > 0", file=sys.stderr)
        return 2

    count = args.count
    if count is None and args.profile == "step":
        count = 3 * args.phase

    conn = connect(Path(args.db).expanduser())
    init_schema(conn)
    i = 0
    try:
        while count is None or i < count:
            value = profile_value(args.profile, i, args.phase)
            ts = int(wall())
            insert_raw(conn, RawSample(ts, None, value, None, None, None, None, None, True))
            print(f"{ts} pm25={value}", flush=True)
            i += 1
            if count is None or i < count:
                sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
