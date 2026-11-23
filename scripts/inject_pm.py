"""
Ghi chuỗi PM2.5 giả vào một DB THỬ để trình diễn bộ điều khiển quạt (control.demo: true).

Mỗi --interval giây ghi một mẫu raw_readings với ts = giờ hiện tại, nên controller
đang chạy với cùng db_path sẽ phản ứng theo thời gian thực.

Hồ sơ:
  high   80 µg/m³ liên tục   (vượt ngưỡng vào 55)
  low    20 µg/m³ liên tục   (dưới ngưỡng thoát 35)
  danger 200 µg/m³ liên tục  (mức khẩn cấp >= 150)
  step   20 -> 80 -> 20, mỗi pha --phase mẫu

Kịch bản nguồn bụi (--scenario normal|cooking|pollution|smoking): sinh một phiên bằng
training/simulate_sources.py, ghi ngay lịch sử nền 15 phút trước đó vào fast_readings
(để bộ nhận diện có đủ bộ đệm), rồi phát lại MỖI GIÂY một mẫu fast_readings (mỗi 10 giây
một mẫu raw_readings) theo giờ thật, để controller chạy bộ nhận diện nguồn bụi trên DB thử.

Ví dụ:
  python scripts/inject_pm.py --db ~/iot_data/demo.db --profile step --phase 12
  python scripts/inject_pm.py --db ~/iot_data/demo.db --scenario smoking --seed 1
Từ chối ghi vào DB thật /home/admin/iot_data/aiot.db trừ khi có --i-know.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiot.db import RawSample, connect, init_schema, insert_fast_many, insert_raw  # noqa: E402

SCENARIOS = ("normal", "cooking", "pollution", "smoking")
HISTORY_S = 900

PRODUCTION_DB = "/home/admin/iot_data/aiot.db"
CONSTANT = {"high": 80.0, "low": 20.0, "danger": 200.0}
PROFILES = (*CONSTANT, "step")


def profile_value(profile: str, i: int, phase: int) -> float:
    """Giá trị PM2.5 của mẫu thứ i (đếm từ 0)."""
    if profile in CONSTANT:
        return CONSTANT[profile]
    # step: pha 0 thấp, pha 1 cao, từ pha 2 trở đi thấp
    return 80.0 if i // phase == 1 else 20.0


def scenario_session(name: str, seed: int):
    """(dict mảng 1 giây, chỉ số giây bắt đầu phát lại) của một kịch bản."""
    import numpy as np

    from training.simulate_sources import simulate_session
    rng = np.random.default_rng(seed)
    bg = "high" if name == "pollution" else "clean"
    events = [name] if name in ("cooking", "smoking") else []
    arr, meta = simulate_session(rng, duration_s=3600, bg_mode=bg, events=events)
    start = meta["events"][0]["start"] - 30 if meta["events"] else HISTORY_S
    return arr, max(start, 60)


def _val(arr, key, i):
    v = float(arr[key][i])
    return None if v != v else v                 # NaN -> None


def run_scenario(conn, name, seed, duration_s, wall, sleep) -> None:
    from aiot.db import FAST_COLUMNS
    arr, start = scenario_session(name, seed)
    now = int(wall())
    hist_from = max(0, start - HISTORY_S)

    def row(i, ts):
        return (ts, {c: _val(arr, c, i) for c in FAST_COLUMNS}, True)

    insert_fast_many(conn, [row(i, now - (start - i)) for i in range(hist_from, start)])
    for i in range(hist_from, start, 10):
        ts = now - (start - i)
        insert_raw(conn, RawSample(ts, _val(arr, "pm1", i), _val(arr, "pm25", i), _val(arr, "pm10", i),
                                   _val(arr, "temp", i), _val(arr, "hum", i), _val(arr, "lux", i), True))
    print(f"Đã ghi {start - hist_from} giây lịch sử; phát lại kịch bản {name}", flush=True)
    end = min(arr["pm25"].size, start + duration_s)
    for i in range(start, end):
        ts = int(wall())
        insert_fast_many(conn, [row(i, ts)])
        if (i - start) % 10 == 0:
            insert_raw(conn, RawSample(ts, _val(arr, "pm1", i), _val(arr, "pm25", i), _val(arr, "pm10", i),
                                       _val(arr, "temp", i), _val(arr, "hum", i), _val(arr, "lux", i), True))
            print(f"{ts} pm25={_val(arr, 'pm25', i)} nhãn={('normal', 'cooking', 'pollution', 'smoking')[arr['label'][i]]}",
                  flush=True)
        sleep(1.0)


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
    parser.add_argument("--scenario", choices=SCENARIOS, help="phát lại kịch bản nguồn bụi 1 giây")
    parser.add_argument("--seed", type=int, default=0, help="seed của kịch bản")
    parser.add_argument("--duration", type=int, default=900, help="giây phát lại kịch bản")
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
    if args.scenario:
        try:
            run_scenario(conn, args.scenario, args.seed, args.duration, wall, sleep)
        except KeyboardInterrupt:
            pass
        finally:
            conn.close()
        return 0
    i = 0
    try:
        while count is None or i < count:
            value = profile_value(args.profile, i, args.phase)
            ts = int(wall())
            insert_raw(conn, RawSample(ts, None, value, None, None, None, None, True))
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
