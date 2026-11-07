"""
Tải dữ liệu công khai theo giờ cho Hà Nội từ Open-Meteo (không cần API key) để huấn luyện
sơ bộ khi dữ liệu của trạm còn quá ngắn (cold start).

- Air Quality API: pm2_5, pm10 (µg/m³). Đây là đầu ra mô hình CAMS, không phải số đo trạm.
  Dữ liệu toàn cầu CAMS của Open-Meteo bắt đầu khoảng 08/2022; trước đó toàn null.
- Historical Weather API: temperature_2m (°C), relative_humidity_2m (%),
  wind_speed_10m (km/h), precipitation (mm).
- Tải theo từng năm rồi ghép theo cột time (giờ địa phương Asia/Ho_Chi_Minh, UTC+7).
  Bỏ phần đầu/cuối mà pm2_5 toàn null; giờ thiếu ở giữa để trống (không nội suy).
- Giấy phép: CC BY 4.0, ghi nguồn "Open-Meteo.com" (xem training/data/SOURCE.md).

Chạy: python training/fetch_public.py [--start 2022-08-01] [--end hôm qua]
"""
import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import pandas as pd
import requests

LAT, LON = 21.0285, 105.8542                  # Hoàn Kiếm, Hà Nội
TZ = "Asia/Ho_Chi_Minh"
AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
WX_URL = "https://archive-api.open-meteo.com/v1/archive"
AQ_VARS = ["pm2_5", "pm10"]
WX_VARS = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "precipitation"]
DEFAULT_START = "2022-08-01"
OUT = Path(__file__).resolve().parent / "data" / "hanoi_openmeteo.csv"


def fetch(url: str, variables, start: dt.date, end: dt.date, retries: int = 3) -> pd.DataFrame:
    """Một lần gọi API cho đoạn [start, end]; trả DataFrame cột time + variables."""
    params = {
        "latitude": LAT, "longitude": LON, "timezone": TZ,
        "hourly": ",".join(variables),
        "start_date": start.isoformat(), "end_date": end.isoformat(),
    }
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                hourly = r.json()["hourly"]
                return pd.DataFrame({"time": hourly["time"],
                                     **{v: hourly[v] for v in variables}})
            msg = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            msg = str(e)
        print(f"  lỗi lần {attempt} ({url}, {start}..{end}): {msg}", file=sys.stderr)
        time.sleep(5 * attempt)
    raise RuntimeError(f"không tải được {url} cho {start}..{end}")


def fetch_range(url: str, variables, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Tải theo từng năm để mỗi lần gọi không quá lớn."""
    parts, a = [], start
    while a <= end:
        b = min(dt.date(a.year, 12, 31), end)
        print(f"  {url.split('/')[2]} {a}..{b}")
        parts.append(fetch(url, variables, a, b))
        a = b + dt.timedelta(days=1)
        time.sleep(1)                          # lịch sự với API miễn phí
    return pd.concat(parts, ignore_index=True)


def build(start: dt.date, end: dt.date) -> pd.DataFrame:
    aq = fetch_range(AQ_URL, AQ_VARS, start, end)
    wx = fetch_range(WX_URL, WX_VARS, start, end)
    df = aq.merge(wx, on="time", how="outer").sort_values("time")
    df = df.rename(columns={"time": "timestamp", "pm2_5": "pm25", "temperature_2m": "temp",
                            "relative_humidity_2m": "hum", "wind_speed_10m": "wind",
                            "precipitation": "precip"})
    ok = df["pm25"].notna().to_numpy()
    if not ok.any():
        raise RuntimeError("API không trả giá trị pm2_5 nào trong khoảng đã chọn")
    first, last = ok.argmax(), len(ok) - 1 - ok[::-1].argmax()
    df = df.iloc[first:last + 1]
    # Thêm độ lệch giờ để đọc lại ra DatetimeIndex có múi giờ
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(TZ)
    return df[["timestamp", "pm25", "pm10", "temp", "hum", "wind", "precip"]]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Tải dữ liệu Open-Meteo theo giờ cho Hà Nội")
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--end", default=(dt.date.today() - dt.timedelta(days=1)).isoformat())
    p.add_argument("--out", default=str(OUT))
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):        # console Windows mặc định cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    df = build(start, end)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    n_missing = int(df["pm25"].isna().sum())
    print(f"đã ghi {len(df)} giờ ({df['timestamp'].iloc[0]} .. {df['timestamp'].iloc[-1]}), "
          f"pm25 thiếu {n_missing} giờ -> {out}")
    print(f"ngày tải: {dt.date.today().isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
