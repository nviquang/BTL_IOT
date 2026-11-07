"""
Dịch vụ collector: đọc mọi cảm biến mỗi 10 giây, ghi SQLite + CSV, gửi MQTT.

- raw_readings (SQLite): mọi mẫu có ít nhất một giá trị đo, kể cả khi DHT11 lỗi
  hay đồng hồ chưa đồng bộ (clock_synced=0). ts là giây epoch kiểu số nguyên.
- CSV: giữ đúng định dạng của pi_sensor_client.py, chỉ ghi khi có đủ DHT11 và lux.
- MQTT không bắt buộc: tắt trong config, thiếu mật khẩu hay mất broker thì vẫn thu thập.
- MQ135 cần sấy: mq135_do = None trong 180 giây đầu sau khi dịch vụ chạy.
- Gộp trung bình 5 phút mỗi phút (chỉ khi đồng hồ đã đồng bộ NTP);
  xoá raw_readings cũ hơn 7 ngày mỗi ngày một lần.

Thay thế pi_sensor_client.py; không chạy cả hai cùng lúc vì dùng chung cảm biến.
Chạy: cd ~/BTL_IOT && venv/bin/python -m aiot.collector
"""
import csv
import json
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
from aiot.db import RawSample, connect, init_schema, insert_raw, purge_raw
from aiot.payload import CSV_HEADER, build_csv_row, build_payload

log = logging.getLogger("aiot.collector")

INTERVAL = 10                   # giây giữa 2 lần đọc, như client cũ
AGGREGATE_EVERY_S = 60
PURGE_EVERY_S = 86400
RAW_KEEP_S = 7 * 86400          # giữ mẫu thô 7 ngày
MQ135_WARMUP_S = 180
TOPIC_SENSOR = "nha/pi01/sensor"
TOPIC_STATUS = "nha/pi01/sensor_status"


# ---------------- Thành phần nhỏ ----------------
class Warmup:
    """Đếm thời gian sấy theo đồng hồ monotonic (không bị ảnh hưởng khi NTP chỉnh giờ)."""

    def __init__(self, duration_s: float, clock=time.monotonic):
        self._clock = clock
        self._until = clock() + duration_s

    def ready(self) -> bool:
        return self._clock() >= self._until


class CsvLog:
    """Ghi nối tiếp CSV định dạng cũ; header chỉ ghi khi file chưa có hoặc rỗng."""

    def __init__(self, path: str, device_id: str):
        self.path = path
        self.device_id = device_id

    def append(self, row: dict) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        is_new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(CSV_HEADER)
            writer.writerow([row[k] for k in CSV_HEADER])


class NullPublisher:
    """Dùng khi tắt MQTT."""

    def publish(self, payload: dict) -> None:
        pass

    def close(self) -> None:
        pass


class MqttPublisher:
    """paho-mqtt v2, tự kết nối lại; trạng thái online/offline giữ lại (retain) + last will."""

    def __init__(self, host: str, port: int, user: str, password: str):
        import paho.mqtt.client as mqtt  # import muộn: laptop test không cần paho

        def on_connect(client, userdata, flags, reason_code, properties):
            if reason_code.is_failure:
                log.warning("Kết nối MQTT thất bại: %s", reason_code)
                return
            log.info("Đã kết nối broker %s:%s", host, port)
            client.publish(TOPIC_STATUS, "online", qos=1, retain=True)

        def on_disconnect(client, userdata, flags, reason_code, properties):
            log.warning("Mất kết nối MQTT, đang thử lại")

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pi01_sensor")
        self._client.username_pw_set(user, password)
        self._client.on_connect = on_connect
        self._client.on_disconnect = on_disconnect
        self._client.will_set(TOPIC_STATUS, "offline", qos=1, retain=True)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.connect_async(host, port, keepalive=60)
        self._client.loop_start()

    def publish(self, payload: dict) -> None:
        if self._client.is_connected():
            self._client.publish(TOPIC_SENSOR, json.dumps(payload), qos=1)

    def close(self) -> None:
        try:
            if self._client.is_connected():
                self._client.publish(TOPIC_STATUS, "offline", qos=1, retain=True).wait_for_publish(timeout=3)
        except Exception as e:
            log.warning("Không gửi được trạng thái offline: %s", e)
        self._client.loop_stop()
        self._client.disconnect()


def is_clock_synced() -> bool:
    """Đồng hồ đã đồng bộ NTP chưa (timedatectl). Không có timedatectl thì coi như chưa."""
    try:
        r = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() == "yes"
    except Exception:
        return False


def _safe_read(name: str, fn):
    """Đọc một cảm biến; mọi lỗi coi như không có giá trị ở mẫu này."""
    try:
        return fn()
    except Exception as e:
        log.warning("Lỗi đọc %s: %s", name, e)
        return None


# ---------------- Một lần thu thập ----------------
def collect_once(sensors, conn, csv_writer, publisher, now: datetime, clock_synced: bool,
                 *, mq135_ready: bool = True) -> RawSample:
    """Đọc mọi cảm biến một lần, ghi raw_readings, CSV và gửi MQTT.

    now là giờ địa phương (naive datetime); ts trong SQLite là int(now.timestamp()).
    Không lỗi nào ở đây được làm dừng vòng lặp: cảm biến, DB, CSV, broker đều được bắt.
    """
    dht = _safe_read("DHT11", sensors.read_dht)
    lux = _safe_read("BH1750", sensors.read_lux)
    pm = _safe_read("PMS7003", sensors.read_pm)
    mq = _safe_read("MQ135", sensors.read_mq135) if mq135_ready else None

    temp, hum = (float(dht[0]), float(dht[1])) if dht is not None else (None, None)
    lux = float(lux) if lux is not None else None
    pm1, pm25, pm10 = (float(pm.pm1), float(pm.pm25), float(pm.pm10)) if pm is not None else (None, None, None)
    mq = int(mq) if mq is not None else None

    sample = RawSample(int(now.timestamp()), pm1, pm25, pm10, temp, hum, lux, mq, bool(clock_synced))
    values = (pm1, pm25, pm10, temp, hum, lux, mq)
    if all(v is None for v in values):
        log.warning("Mọi cảm biến đều lỗi, bỏ qua mẫu")
        return sample

    try:
        insert_raw(conn, sample)
    except sqlite3.Error as e:
        log.error("Lỗi ghi SQLite: %s", e)

    if temp is None or lux is None:
        # Không ghi dòng thiếu vào CSV: notebook time series không chịu được NaN
        log.info("Bỏ qua CSV/MQTT (DHT11 hoặc BH1750 lỗi), PM2.5=%s", pm25)
        return sample

    row = build_csv_row(now, csv_writer.device_id, temp, hum, lux)
    try:
        csv_writer.append(row)
    except OSError as e:
        log.error("Lỗi ghi CSV: %s", e)
    try:
        publisher.publish(build_payload(row, pm1, pm25, pm10, mq))
    except Exception as e:
        log.warning("Lỗi gửi MQTT: %s", e)
    log.info("T=%s H=%s Lux=%s PM2.5=%s MQ135=%s", row["temperature_c"], row["humidity_pct"],
             row["lux"], pm25, mq)
    return sample


# ---------------- Vòng lặp dịch vụ ----------------
def run(sensors, conn, csv_writer, publisher, stop: threading.Event,
        *, clock=time.monotonic, synced=is_clock_synced) -> None:
    """Thu thập mỗi INTERVAL giây tới khi stop được đặt (SIGTERM/SIGINT)."""
    warmup = Warmup(MQ135_WARMUP_S, clock=clock)
    next_time = clock()
    next_aggregate = clock() + AGGREGATE_EVERY_S
    next_purge = clock() + AGGREGATE_EVERY_S       # xoá lần đầu sau khi chạy 1 phút
    clock_ok = synced()
    if not clock_ok:
        log.warning("Đồng hồ chưa đồng bộ NTP: mẫu được đánh dấu clock_synced=0, chưa gộp")

    while not stop.is_set():
        collect_once(sensors, conn, csv_writer, publisher, datetime.now(), clock_ok,
                     mq135_ready=warmup.ready())

        if clock() >= next_aggregate:
            next_aggregate = clock() + AGGREGATE_EVERY_S
            clock_ok = synced()
            if clock_ok:
                try:
                    aggregate_pending(conn, int(time.time()))
                except sqlite3.Error as e:
                    log.error("Lỗi gộp 5 phút: %s", e)
                if clock() >= next_purge:
                    next_purge = clock() + PURGE_EVERY_S
                    try:
                        purge_raw(conn, int(time.time()) - RAW_KEEP_S)
                    except sqlite3.Error as e:
                        log.error("Lỗi xoá mẫu thô cũ: %s", e)

        # Giữ nhịp đều đúng INTERVAL giây
        next_time += INTERVAL
        delay = next_time - clock()
        if delay > 0:
            stop.wait(delay)
        else:
            next_time = clock()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from aiot.config import load_config
    from aiot.sensors import PiSensors

    root = Path(__file__).resolve().parent.parent
    cfg = load_config(root / "config.yaml", root / ".env")

    stop = threading.Event()

    def on_signal(signum, frame):
        log.info("Nhận tín hiệu %s, đang dừng", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    conn = connect(cfg.db_path)
    init_schema(conn)
    csv_writer = CsvLog(cfg.csv_path, cfg.device_id)

    publisher = NullPublisher()
    if not cfg.mqtt.enabled:
        log.info("MQTT đang tắt trong config.yaml, chỉ ghi SQLite và CSV")
    elif cfg.secrets.mqtt_password is None:
        log.warning("Chưa có MQTT_PASSWORD trong .env, bỏ qua MQTT")
    else:
        try:
            publisher = MqttPublisher(cfg.mqtt.host, cfg.mqtt.port, cfg.mqtt.user, cfg.secrets.mqtt_password)
        except Exception as e:
            log.warning("Không khởi tạo được MQTT (%s), chỉ ghi SQLite và CSV", e)

    sensors = PiSensors()
    log.info("Collector bắt đầu: db=%s csv=%s", cfg.db_path, cfg.csv_path)
    try:
        run(sensors, conn, csv_writer, publisher, stop)
    finally:
        publisher.close()
        sensors.close()
        conn.close()
        log.info("Collector đã dừng")


if __name__ == "__main__":
    main()
