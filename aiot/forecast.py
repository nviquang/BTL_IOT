"""
Dự báo PM2.5 trung bình giờ cho 6 giờ tới bằng mô hình đối chứng (Task 9).

- Chỉ dùng thư viện chuẩn và numpy (Pi không cài pandas); numpy chỉ được nạp khi cần,
  nên controller và bot import module này mà không tốn thêm thời gian khởi động.
- Chuỗi giờ lấy từ bảng readings (TB 5 phút): giờ nào có ít nhất 6/12 cửa sổ hợp lệ thì
  lấy trung bình, ngược lại là None. Mốc giờ là epoch chia hết cho 3600.
- made_at = đầu giờ hiện tại; horizon k (1..6) là giờ thứ k sau giờ đủ cuối cùng, tức
  giờ bắt đầu lúc made_at + (k - 1) * 3600.
- Mô hình: 'seasonal_naive' (cùng giờ hôm qua) khi có đủ 24 giờ đầu vào sau nội suy,
  ngược lại 'persistence' (giá trị giờ hợp lệ gần nhất lặp lại 6 lần). predictor (LSTM,
  Task 12) nếu có thì dùng trước, model 'lstm'; lỗi hoặc kết quả sai thì về đối chứng.
- Giờ hợp lệ mới nhất đã kết thúc quá 3 giờ: không dự báo, không ghi gì, không raise.
- Ghi 6 dòng bằng INSERT OR REPLACE trong một transaction (chạy lại cùng giờ thì ghi đè).
- main() bỏ qua lần chạy (thoát 0) khi đồng hồ chưa đồng bộ NTP, để lần chạy bù lúc khởi
  động không ghi dự báo với giờ sai.

Chạy một lần: cd ~/BTL_IOT && venv/bin/python -m aiot.forecast (timer mỗi giờ, phút 02).
"""
import argparse
import logging
import math
import time
from pathlib import Path
from typing import Callable

from aiot.collector import is_clock_synced

log = logging.getLogger("aiot.forecast")

HOUR_S = 3600
HORIZONS = 6
WINDOWS_PER_HOUR = HOUR_S // 300        # 12 cửa sổ 5 phút
MIN_WINDOWS = 6                         # giờ cần ít nhất 6/12 cửa sổ hợp lệ
INPUT_HOURS = 24
MAX_DATA_AGE_S = 3 * HOUR_S             # dữ liệu cũ hơn thì không dự báo

Series = list[tuple[int, float | None]]

# Tên hiển thị của mô hình (bot /forecast và báo cáo sáng)
MODEL_NAMES = {
    "persistence": "giữ nguyên giờ gần nhất",
    "seasonal_naive": "cùng giờ hôm qua",
    "lstm": "mạng LSTM",
}


# ---------------- dữ liệu vào ----------------
def hourly_series(conn, end_ts: int, hours: int = 48) -> Series:
    """PM2.5 trung bình theo giờ, `hours` giờ đủ kết thúc trước hoặc đúng end_ts.

    Trả về [(đầu giờ, giá trị hoặc None)] tăng dần; phần tử cuối là giờ đủ cuối cùng.
    Chỉ dùng dòng readings valid=1 có pm25; giờ có ít hơn MIN_WINDOWS dòng thì None."""
    last = int(end_ts) // HOUR_S * HOUR_S - HOUR_S
    first = last - (hours - 1) * HOUR_S
    rows = conn.execute(
        "SELECT (ts / ?) * ?, AVG(pm25), COUNT(*) FROM readings "
        "WHERE valid = 1 AND pm25 IS NOT NULL AND ts >= ? AND ts < ? "
        "GROUP BY ts / ?",
        (HOUR_S, HOUR_S, first, last + HOUR_S, HOUR_S),
    ).fetchall()
    means = {int(h): float(avg) for h, avg, n in rows if n >= MIN_WINDOWS}
    return [(t, means.get(t)) for t in range(first, last + HOUR_S, HOUR_S)]


def prepare_input(series: Series, hours: int = INPUT_HOURS, max_gap_h: int = 2,
                  min_valid: int = 20):
    """numpy.ndarray `hours` giá trị (giờ đủ cuối cùng ở cuối) hoặc None.

    Khoảng trống tối đa max_gap_h giờ liên tiếp được nội suy tuyến tính; khoảng trống đầu
    chuỗi (không có điểm bên trái) tối đa max_gap_h giờ lấy giá trị hợp lệ đầu tiên.
    None khi: chưa đủ `hours` điểm, có khoảng trống dài hơn, ít hơn min_valid điểm hợp lệ,
    hoặc điểm cuối bị thiếu (không có điểm bên phải để nội suy)."""
    if len(series) < hours:
        return None
    values = [v for _, v in series[-hours:]]
    if values[-1] is None or sum(v is not None for v in values) < min_valid:
        return None
    run = 0
    for v in values:
        run = run + 1 if v is None else 0
        if run > max_gap_h:
            return None

    import numpy as np
    x = np.array([math.nan if v is None else float(v) for v in values], dtype=float)
    known = ~np.isnan(x)
    idx = np.arange(hours)
    # np.interp giữ giá trị biên cho phần đầu thiếu (không có điểm bên trái)
    x[~known] = np.interp(idx[~known], idx[known], x[known])
    return x


# ---------------- mô hình đối chứng ----------------
def _last_valid(series: Series):
    for ts, v in reversed(series):
        if v is not None:
            return ts, v
    return None


def persistence(series: Series) -> list[float]:
    """Giá trị giờ hợp lệ gần nhất lặp lại 6 lần. Chuỗi không có giá trị nào: ValueError."""
    found = _last_valid(series)
    if found is None:
        raise ValueError("chuỗi PM2.5 không có giờ hợp lệ")
    return [float(found[1])] * HORIZONS


def seasonal_naive(x) -> list[float]:
    """Cùng giờ hôm qua. x là đầu vào 24 giờ từ prepare_input (x[-1] là giờ đủ cuối L).

    Mục tiêu horizon k là giờ L + k; 24 giờ trước đó là L + k - 24, tức x[k - 1]."""
    if len(x) < INPUT_HOURS:
        raise ValueError(f"cần {INPUT_HOURS} giờ đầu vào, có {len(x)}")
    start = len(x) - INPUT_HOURS
    return [float(v) for v in x[start:start + HORIZONS]]


def _check_prediction(preds) -> list[float]:
    """Kết quả predictor phải là 6 số hữu hạn; sai thì ValueError."""
    try:
        out = [float(v) for v in preds]
    except TypeError:
        raise ValueError(f"kết quả không phải dãy số: {preds!r}") from None
    if len(out) != HORIZONS:
        raise ValueError(f"cần {HORIZONS} giá trị, có {len(out)}")
    if not all(math.isfinite(v) for v in out):
        raise ValueError(f"có giá trị không hữu hạn: {out}")
    return out


# ---------------- chạy dự báo ----------------
def run_forecast(conn, now: int,
                 predictor: Callable[..., list[float]] | None = None
                 ) -> tuple[str, list[float]] | None:
    """Dự báo 6 giờ tới lúc now, ghi vào bảng forecasts. Trả về (model, 6 giá trị),
    hoặc None nếu không đủ dữ liệu (không ghi gì). Lỗi ghi DB thì rollback và raise."""
    now = int(now)
    series = hourly_series(conn, now, hours=INPUT_HOURS)
    found = _last_valid(series)
    if found is None:
        log.info("Chưa có giờ PM2.5 hợp lệ nào trong %d giờ qua: không dự báo", INPUT_HOURS)
        return None
    if now - (found[0] + HOUR_S) > MAX_DATA_AGE_S:
        log.info("Giờ PM2.5 hợp lệ mới nhất kết thúc quá 3 giờ trước: không dự báo")
        return None

    x = prepare_input(series)
    model, preds = None, None
    if predictor is not None:
        if x is None:
            log.warning("Không đủ 24 giờ đầu vào cho mô hình học máy, dùng đối chứng")
        else:
            try:
                preds = _check_prediction(predictor(x))
                model = "lstm"
            except Exception as e:
                log.warning("Mô hình học máy lỗi, dùng đối chứng: %s", e)
                preds = None
    if preds is None:
        if x is not None:
            model, preds = "seasonal_naive", seasonal_naive(x)
        else:
            model, preds = "persistence", persistence(series)
    preds = [max(0.0, float(v)) for v in preds]

    made_at = now // HOUR_S * HOUR_S
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO forecasts (made_at, horizon_h, pm25_pred, model) "
            "VALUES (?, ?, ?, ?)",
            [(made_at, k + 1, v, model) for k, v in enumerate(preds)],
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    log.info("Dự báo made_at=%s model=%s: %s", made_at, model,
             ", ".join(f"{v:.1f}" for v in preds))
    return model, preds


# ---------------- đọc dự báo ----------------
def latest_rows(conn, now: int, max_age_s: int):
    """(made_at, model, [(horizon, giá trị)]) của dự báo mới nhất không ở tương lai,
    hoặc None nếu chưa có hay đã cũ hơn max_age_s."""
    now = int(now)
    row = conn.execute("SELECT MAX(made_at) FROM forecasts WHERE made_at <= ?", (now,)).fetchone()
    made_at = row[0] if row else None
    if made_at is None or now - made_at > max_age_s:
        return None
    rows = conn.execute(
        "SELECT horizon_h, pm25_pred, model FROM forecasts WHERE made_at = ? ORDER BY horizon_h",
        (made_at,),
    ).fetchall()
    model = rows[0][2] if rows else ""
    return int(made_at), model, [(int(h), float(v)) for h, v, _ in rows]


def latest_forecast(conn, now: int, max_age_s: int) -> tuple[float, int] | None:
    """(TB dự báo horizon 1–3, made_at) của dự báo mới nhất, hoặc None nếu chưa có,
    đã cũ hơn max_age_s, hoặc thiếu một trong ba horizon đầu."""
    found = latest_rows(conn, now, max_age_s)
    if found is None:
        return None
    made_at, _, rows = found
    first3 = {h: v for h, v in rows if h <= 3}
    if sorted(first3) != [1, 2, 3]:
        return None
    return sum(first3.values()) / 3, made_at


# ---------------- chạy một lần ----------------
def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from aiot.config import load_config
    from aiot.db import connect, init_schema

    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Dự báo PM2.5 6 giờ tới (chạy một lần)")
    parser.add_argument("--config", default=str(root / "config.yaml"))
    args = parser.parse_args(argv)

    cfg = load_config(args.config, root / ".env")
    if not is_clock_synced():
        # Pi không có RTC: trước khi có NTP, giờ có thể là lúc tắt máy lần trước, dự báo ghi
        # với made_at đó sẽ bị controller và /forecast coi là còn mới. Timer chạy lại giờ sau.
        log.warning("Đồng hồ Pi chưa đồng bộ NTP: bỏ lần dự báo này")
        return 0
    conn = connect(cfg.db_path)
    try:
        init_schema(conn)
        run_forecast(conn, int(time.time()))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
