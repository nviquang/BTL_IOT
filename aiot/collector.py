"""
Dịch vụ collector: đọc cảm biến mỗi giây (fast_readings, cho bộ nhận diện nguồn bụi)
và mỗi 10 giây ghi raw_readings và CSV.

- fast_readings (SQLite): mẫu 1 giây gồm PM, 6 kênh đếm hạt, nhiệt/ẩm (DHT11 ~2 giây),
  lux; ghi gộp mỗi 5 giây, giữ 48 giờ (đoạn có nhãn trong source_labels giữ lại).

- raw_readings (SQLite): mọi mẫu có ít nhất một giá trị đo, kể cả khi DHT11 lỗi
  hay đồng hồ chưa đồng bộ (clock_synced=0). ts là giây epoch kiểu số nguyên.
- CSV: ghi nhiệt độ, độ ẩm và ánh sáng khi có đủ số liệu và đồng hồ tin được.
- Gộp trung bình 5 phút mỗi phút (chỉ khi đồng hồ tin được: is_clock_trusted);
  xoá raw_readings cũ hơn 7 ngày mỗi ngày một lần.

Chạy: cd ~/BTL_IOT && venv/bin/python -m aiot.collector
"""
import csv
import logging
import os
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from aiot.aggregate import aggregate_pending
from aiot.db import (RawSample, connect, init_schema, insert_fast_many, insert_raw,
                     purge_fast, purge_raw)

log = logging.getLogger("aiot.collector")

INTERVAL = 10                   # giây giữa 2 mẫu raw_readings/CSV
FAST_INTERVAL = 1               # giây giữa 2 mẫu fast_readings (bộ nhận diện nguồn bụi)
FAST_FLUSH_S = 5
FAST_KEEP_S = 2 * 86400         # giữ mẫu 1 giây 48 giờ (đoạn có nhãn giữ lâu hơn)
AGGREGATE_EVERY_S = 60
PURGE_EVERY_S = 86400
RAW_KEEP_S = 7 * 86400          # giữ mẫu thô 7 ngày
CSV_HEADER = ["timestamp", "device_id", "temperature_c", "humidity_pct", "lux"]


# ---------------- Thành phần nhỏ ----------------
class CsvLog:
    """Ghi nối tiếp CSV định dạng cũ; header chỉ ghi khi file chưa có hoặc rỗng."""

    def __init__(self, path: str, device_id: str):
        self.path = path
        self.device_id = device_id

    def append(self, now: datetime, temp: float, hum: float, lux: float) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        is_new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(CSV_HEADER)
            writer.writerow((now.isoformat(timespec="seconds"), self.device_id,
                             round(temp, 1), round(hum, 1), round(lux, 1)))


SYNC_FLAG_PATH = "/run/systemd/timesync/synchronized"


def is_clock_trusted(flag_path: str = SYNC_FLAG_PATH) -> bool:
    """Đồng hồ Pi có tin được không: đã đồng bộ NTP ít nhất một lần từ lúc khởi động.

    systemd-timesyncd tạo flag_path ở lần đồng bộ đầu tiên và file nằm tới khi khởi động
    lại (/run là tmpfs). Không dùng "NTPSynchronized đang là yes": giá trị đó về "no"
    khoảng 9 giờ sau khi mất Internet (maxerror của kernel > 16 s) dù đồng hồ vẫn chạy
    đúng, làm dừng gộp 5 phút, dự báo và lệnh điều khiển (trái nguyên tắc local-first).
    Chỉ khi không có file cờ mới hỏi timedatectl (ví dụ máy không dùng timesyncd).
    Không có timedatectl thì coi như chưa tin được.
    """
    if os.path.exists(flag_path):
        return True
    try:
        r = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() == "yes"
    except Exception:
        return False


def _rollback(conn) -> None:
    """Huỷ transaction đang mở sau lỗi ghi (ví dụ commit lỗi khi đầy đĩa), để không
    giữ khoá ghi WAL chặn controller và bot."""
    try:
        conn.rollback()
    except Exception as e:
        log.error("Rollback lỗi: %s", e)


def _safe_read(name: str, fn):
    """Đọc một cảm biến; mọi lỗi coi như không có giá trị ở mẫu này."""
    try:
        return fn()
    except Exception as e:
        log.warning("Lỗi đọc %s: %s", name, e)
        return None


def _warn_ts_collision(conn, ts: int) -> None:
    """Chỉ để nhìn thấy: sau khi mất điện, đồng hồ có thể khôi phục về giờ cũ hơn mẫu đã
    lưu, nên INSERT OR IGNORE bỏ mẫu mới trùng ts mà không báo gì."""
    last = conn.execute("SELECT MAX(ts) FROM raw_readings").fetchone()[0]
    if last is not None and ts <= last:
        log.warning("Đồng hồ chưa tin được và ts=%s <= MAX(raw_readings.ts)=%s: mẫu trùng ts "
                    "sẽ bị bỏ (INSERT OR IGNORE)", ts, last)


# ---------------- Một lần thu thập ----------------
def collect_once(sensors, conn, csv_writer, now: datetime,
                 clock_synced: bool) -> RawSample:
    """Đọc mọi cảm biến một lần, ghi raw_readings và CSV.

    now là giờ địa phương (naive datetime); ts trong SQLite là int(now.timestamp()).
    Lỗi đọc cảm biến, ghi SQLite hoặc CSV được ghi log và vòng lặp tiếp tục.
    """
    dht = _safe_read("DHT11", sensors.read_dht)
    lux = _safe_read("BH1750", sensors.read_lux)
    pm = _safe_read("PMS7003", sensors.read_pm)

    temp, hum = (float(dht[0]), float(dht[1])) if dht is not None else (None, None)
    lux = float(lux) if lux is not None else None
    pm1, pm25, pm10 = (float(pm.pm1), float(pm.pm25), float(pm.pm10)) if pm is not None else (None, None, None)

    sample = RawSample(int(now.timestamp()), pm1, pm25, pm10, temp, hum, lux, bool(clock_synced))
    values = (pm1, pm25, pm10, temp, hum, lux)
    if all(v is None for v in values):
        log.warning("Mọi cảm biến đều lỗi, bỏ qua mẫu")
        return sample

    try:
        if not clock_synced:
            _warn_ts_collision(conn, sample.ts)
        insert_raw(conn, sample)
    except sqlite3.Error as e:
        log.error("Lỗi ghi SQLite: %s", e)
        _rollback(conn)

    if not clock_synced:
        # CSV không có cờ đồng hồ nên chỉ ghi SQLite (clock_synced=0).
        log.info("Bỏ qua CSV (đồng hồ chưa tin được), PM2.5=%s", pm25)
        return sample

    if temp is None or lux is None:
        # Không ghi dòng thiếu vào CSV: notebook time series không chịu được NaN
        log.info("Bỏ qua CSV (DHT11 hoặc BH1750 lỗi), PM2.5=%s", pm25)
        return sample

    try:
        csv_writer.append(now, temp, hum, lux)
    except OSError as e:
        log.error("Lỗi ghi CSV: %s", e)
    log.info("T=%.1f H=%.1f Lux=%.1f PM2.5=%s", temp, hum, lux, pm25)
    return sample


# ---------------- Vòng lặp dịch vụ ----------------
def _flush_fast(conn, buffer) -> None:
    """Ghi các mẫu 1 giây đang chờ (gộp một commit mỗi FAST_FLUSH_S để đỡ mòn thẻ nhớ)."""
    if not buffer:
        return
    try:
        insert_fast_many(conn, buffer)
    except sqlite3.Error as e:
        log.error("Lỗi ghi fast_readings: %s", e)
        _rollback(conn)
    buffer.clear()


def run(sensors, conn, csv_writer, stop: threading.Event,
        *, clock=time.monotonic, synced=is_clock_trusted, wall=datetime.now) -> None:
    """Thu thập tới khi stop được đặt (SIGTERM/SIGINT).

    sensors có snapshot() (SensorHub): nhịp FAST_INTERVAL giây ghi fast_readings, mỗi
    INTERVAL giây ghi raw_readings và CSV. Không có snapshot(): chỉ nhịp 10 giây."""
    fast = hasattr(sensors, "snapshot")
    tick = FAST_INTERVAL if fast else INTERVAL
    next_time = clock()
    next_slow = clock()
    next_flush = clock() + FAST_FLUSH_S
    next_aggregate = clock() + AGGREGATE_EVERY_S
    next_purge = clock() + AGGREGATE_EVERY_S       # xoá lần đầu sau khi chạy 1 phút
    buffer = []
    clock_ok = synced()
    if not clock_ok:
        log.warning("Đồng hồ chưa tin được (chưa đồng bộ NTP lần nào từ lúc khởi động): mẫu được "
                    "đánh dấu clock_synced=0, chưa gộp, không ghi CSV")

    while not stop.is_set():
        now_dt = wall()
        if fast:
            snap = sensors.snapshot()
            if any(v is not None for v in snap.values()):
                buffer.append((int(now_dt.timestamp()), snap, clock_ok))
            if clock() >= next_flush:
                next_flush = clock() + FAST_FLUSH_S
                _flush_fast(conn, buffer)
        if clock() >= next_slow:
            next_slow += INTERVAL
            if next_slow <= clock():
                next_slow = clock() + INTERVAL
            collect_once(sensors, conn, csv_writer, now_dt, clock_ok)

        if clock() >= next_aggregate:
            next_aggregate = clock() + AGGREGATE_EVERY_S
            clock_ok = synced()
            if clock_ok:
                try:
                    aggregate_pending(conn, int(time.time()))
                except sqlite3.Error as e:
                    log.error("Lỗi gộp 5 phút: %s", e)
                    _rollback(conn)
                if clock() >= next_purge:
                    next_purge = clock() + PURGE_EVERY_S
                    try:
                        purge_raw(conn, int(time.time()) - RAW_KEEP_S)
                        if fast:
                            purge_fast(conn, int(time.time()) - FAST_KEEP_S)
                    except sqlite3.Error as e:
                        log.error("Lỗi xoá mẫu thô cũ: %s", e)
                        _rollback(conn)

        # Giữ nhịp đều đúng tick giây
        next_time += tick
        delay = next_time - clock()
        if delay > 0:
            stop.wait(delay)
        else:
            next_time = clock()
    _flush_fast(conn, buffer)


def install_signal_handlers(stop, importer=None) -> None:
    """Đặt handler SIGTERM/SIGINT để dừng sạch (stop.set()).

    Blinka (adafruit_dht -> pulseio) khi được import tự đăng ký SIGTERM gọi SystemExit(1),
    đè handler của collector: systemd dừng dịch vụ thì vòng lặp bị cắt ngang và thoát mã 1.
    Vì vậy import thư viện đó trước (nếu có) rồi mới đặt handler của mình. Tiến trình
    libgpiod_pulsein vẫn được dọn: sensors.close() gọi dht.exit(), và atexit của Blinka.
    """
    import importlib

    load = importer or importlib.import_module
    try:
        load("adafruit_dht")
    except Exception:
        pass                    # laptop/thiếu thư viện: không có handler nào để đè

    def on_signal(signum, frame):
        log.info("Nhận tín hiệu %s, đang dừng", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from aiot.config import load_config
    from aiot.sensors import PiSensors, SensorHub

    root = Path(__file__).resolve().parent.parent
    cfg = load_config(root / "config.yaml", root / ".env")

    stop = threading.Event()
    install_signal_handlers(stop)

    conn = connect(cfg.db_path)
    init_schema(conn)
    csv_writer = CsvLog(cfg.csv_path, cfg.device_id)

    sensors = PiSensors()
    hub = SensorHub(sensors)
    hub.start()
    log.info("Collector bắt đầu: db=%s csv=%s (mẫu 1 giây + mẫu 10 giây)", cfg.db_path, cfg.csv_path)
    try:
        run(hub, conn, csv_writer, stop)
    finally:
        hub.stop()
        sensors.close()
        conn.close()
        log.info("Collector đã dừng")


if __name__ == "__main__":
    main()
