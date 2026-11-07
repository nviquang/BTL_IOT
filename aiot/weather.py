"""
Cảnh báo "nắng gắt" và "có thể sắp mưa" từ cảm biến ngoài ban công, kèm ảnh camera.

Đây là ước lượng từ DHT11 + BH1750, không phải dự báo thời tiết.

- Luật (tham số trong mục weather của config.yaml), trên bảng readings (trung bình 5 phút):
  * weather_sunny: lux > sunny_lux VÀ nhiệt độ >= sunny_temp_c.
  * weather_rain: độ ẩm >= rain_hum_pct VÀ lux < rain_lux, chỉ khi giờ địa phương hiện tại
    thuộc [day_start_h, day_end_h) (ban đêm lux luôn thấp).
- Một trạng thái "bật" khi điều kiện đúng ở sustain_windows cửa sổ 5 phút gần nhất có đủ
  trường cần thiết, các cửa sổ liền nhau (cách nhau đúng 300 s) và cửa sổ mới nhất bắt đầu
  không quá MAX_AGE_S trước now. Không dùng cờ valid (chỉ nói về PM2.5).
  Thiếu dữ liệu thì không kết luận (None) và giữ nguyên trạng thái đã nhớ.
- Chỉ báo khi chuyển sang "bật" (khoá state 'weather_state:<kind>' = "1"/"0"), tối đa
  1 tin mỗi cooldown_s cho từng loại (khoá 'alert_last:<kind>'), theo giờ yên lặng chung
  (aiot.notifier.quiet_window). Bị chặn (cooldown hoặc giờ yên lặng) thì không ghi nhận
  "bật": tin được gửi ở lần chạy ĐẦU TIÊN được phép mà điều kiện vẫn đúng (ví dụ nắng gắt
  bắt đầu lúc 21h50 với /quiet 22 6 thì báo ngay; bắt đầu lúc 22h10 và còn nắng lúc 6h thì
  báo lúc 6h01).
- Khoá 'weather_seen:<kind>' ghi lần cuối có đủ dữ liệu để kết luận. "Bật" đã nhớ mà lần
  cuối có dữ liệu cũ hơn cooldown_s (collector dừng, cảm biến lỗi lâu) thì bị coi như chưa
  biết, nên điều kiện quay lại sẽ được báo lại.
- Khi báo: chụp ảnh (ngoài transaction, trong tiến trình con có timeout, xem
  aiot.camera.capture_subprocess), rồi ghi tin + ảnh vào outbox và cập nhật khoá trong MỘT
  transaction. Chụp lỗi thì gửi chữ kèm " (không chụp được ảnh)".

Chạy một lần (aiot-weather.timer mỗi 5 phút): cd ~/BTL_IOT && venv/bin/python -m aiot.weather
Đồng hồ chưa tin được (aiot.collector.is_clock_trusted): bỏ qua cả đánh giá lẫn xoá ảnh cũ
(giờ trong ngày và tuổi file đều có thể sai). Luôn thoát 0.
"""
import argparse
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from aiot.camera import capture_subprocess, purge_photos
from aiot.collector import is_clock_trusted
from aiot.config import load_config
from aiot.db import get_state, set_state
from aiot.notifier import enqueue, in_quiet_hours, quiet_window

log = logging.getLogger("aiot.weather")

WINDOW_S = 300
MAX_AGE_S = 600                     # cửa sổ mới nhất phải bắt đầu trong 10 phút gần đây
KINDS = ("weather_sunny", "weather_rain")
NO_PHOTO = " (không chụp được ảnh)"
DISCLAIMER = "(Đây là ước lượng từ cảm biến ngoài ban công, không phải dự báo thời tiết.)"
_FIELDS = {"weather_sunny": ("lux", "temp"), "weather_rain": ("hum", "lux")}


class Observation(NamedTuple):
    active: bool
    lux: float | None
    temp: float | None
    hum: float | None


def _state_key(kind):
    return f"weather_state:{kind}"


def _last_key(kind):
    return f"alert_last:{kind}"


def _seen_key(kind):
    return f"weather_seen:{kind}"


def _int_or_none(text):
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def _condition(kind, lux, temp, hum, w):
    if kind == "weather_sunny":
        return lux > w.sunny_lux and temp >= w.sunny_temp_c
    return hum >= w.rain_hum_pct and lux < w.rain_lux


def _observe(conn, kind, now, w):
    fields = _FIELDS[kind]
    n = max(1, int(w.sustain_windows))
    where = " AND ".join(f"{f} IS NOT NULL" for f in fields)
    rows = conn.execute(
        f"SELECT ts, lux, temp, hum FROM readings WHERE ts <= ? AND {where} "
        f"ORDER BY ts DESC LIMIT ?", (int(now), n)).fetchall()
    if len(rows) < n or now - rows[0][0] > MAX_AGE_S:
        return None
    if any(rows[i][0] - rows[i + 1][0] != WINDOW_S for i in range(n - 1)):
        return None
    active = all(_condition(kind, lux, temp, hum, w) for _, lux, temp, hum in rows)
    if kind == "weather_rain":
        hour = datetime.fromtimestamp(int(now)).hour
        active = active and w.day_start_h <= hour < w.day_end_h
    _, lux, temp, hum = rows[0]
    return Observation(active, lux, temp, hum)


def evaluate(conn, now, w):
    """{kind: Observation | None} cho mọi loại. Giá trị số lấy từ cửa sổ mới nhất."""
    return {kind: _observe(conn, kind, int(now), w) for kind in KINDS}


def _fmt(value, digits):
    return f"{value:.{digits}f}".replace(".", ",")


def message(kind, obs):
    if kind == "weather_sunny":
        head = (f"Trời đang nắng gắt ({_fmt(obs.lux, 0)} lux, {_fmt(obs.temp, 1)} °C). "
                f"Ra ngoài nhớ mang áo chống nắng.")
    else:
        head = (f"Trời tối và ẩm ({_fmt(obs.hum, 0)}%, {_fmt(obs.lux, 0)} lux), "
                f"có thể sắp mưa. Nhớ mang áo mưa.")
    return f"{head}\n{DISCLAIMER}"


def _allowed(conn, kind, now, cfg):
    last = get_state(conn, _last_key(kind))
    try:
        if last is not None and now - int(last) < cfg.weather.cooldown_s:
            return False
    except ValueError:
        log.warning("Khoá %s sai định dạng, bỏ qua", _last_key(kind))
    start, end = quiet_window(conn, cfg)
    return not in_quiet_hours(datetime.fromtimestamp(now).hour, start, end)


def _photo_path(cfg, kind, now):
    name = f"{datetime.fromtimestamp(now):%Y%m%d_%H%M%S}_{kind}.jpg"
    return os.path.join(cfg.weather.photo_dir, name)


def run_once(conn, now, cfg, *, capture=capture_subprocess):
    """Đánh giá luật, ghi tin cho những loại vừa chuyển sang "bật". Trả về danh sách kind
    đã ghi vào outbox."""
    now = int(now)
    sent = []
    for kind, obs in evaluate(conn, now, cfg.weather).items():
        if obs is None:
            continue                                    # thiếu dữ liệu: giữ trạng thái
        previous = get_state(conn, _state_key(kind))
        seen = _int_or_none(get_state(conn, _seen_key(kind)))
        if previous == "1" and (seen is None or now - seen > cfg.weather.cooldown_s):
            log.info("Trạng thái %s đã nhớ quá cũ (mất dữ liệu lâu), coi như chưa biết", kind)
            previous = None
        set_state(conn, _seen_key(kind), now)
        if not obs.active:
            if previous != "0":
                set_state(conn, _state_key(kind), "0")
            continue
        if previous == "1" or not _allowed(conn, kind, now, cfg):
            continue
        text = message(kind, obs)
        path = _photo_path(cfg, kind, now)
        try:
            ok = bool(capture(path, index=cfg.weather.camera_index))
        except Exception:
            log.exception("Lỗi khi chụp ảnh")
            ok = False
        if ok:
            photo = path
        else:
            photo = None
            text += NO_PHOTO
        try:
            enqueue(conn, kind, text, now, photo_path=photo, commit=False)
            set_state(conn, _state_key(kind), "1", commit=False)
            set_state(conn, _last_key(kind), now, commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        log.info("Đã ghi cảnh báo %s%s", kind, "" if ok else " (không có ảnh)")
        sent.append(kind)
    return sent


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Cảnh báo nắng gắt / có thể mưa (chạy một lần)")
    parser.add_argument("--config", default=str(root / "config.yaml"))
    parser.add_argument("--env", default=str(root / ".env"))
    args = parser.parse_args(argv)
    try:
        from aiot.db import connect, init_schema

        cfg = load_config(args.config, args.env)
        if not is_clock_trusted():
            log.warning("Đồng hồ Pi chưa đồng bộ NTP: bỏ qua lần đánh giá này")
            return 0
        now = int(time.time())
        conn = connect(cfg.db_path)
        try:
            init_schema(conn)
            run_once(conn, now, cfg)
        finally:
            conn.close()
        removed = purge_photos(cfg.weather.photo_dir, cfg.weather.photo_keep_days, now)
        if removed:
            log.info("Đã xoá %d ảnh cũ", removed)
    except Exception:
        log.exception("Lỗi khi chạy cảnh báo thời tiết")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
