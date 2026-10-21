"""
CLIENT cảm biến chạy trên Raspberry Pi 4
- Đọc DHT11 (nhiệt độ + độ ẩm) ở chân 11 (GPIO17)
- Đọc BH1750 (ánh sáng) qua I2C ở chân 3 (SDA/GPIO2) và chân 5 (SCL/GPIO3)
- Mỗi INTERVAL giây ghi 1 dòng vào CSV trên thẻ nhớ + gửi JSON lên broker MQTT

Chạy: ~/mqtt-env/bin/python ~/pi_sensor_client.py
"""
import csv
import json
import os
import time
from datetime import datetime

import adafruit_bh1750
import adafruit_dht
import board
import busio
import paho.mqtt.client as mqtt

# Dò pulseio GIỐNG HỆT logic của adafruit_dht để HAS_PULSEIO khớp
# với đường đọc mà thư viện thực sự sẽ dùng.
try:
    from pulseio import PulseIn  # noqa: F401

    HAS_PULSEIO = True
except (ImportError, NotImplementedError):
    HAS_PULSEIO = False

# ===================== CẤU HÌNH =====================
BROKER = "10.208.20.254"        # IP laptop (chạy ipconfig để kiểm tra)
PORT = 1883
USER = "pi01"
PASSWORD = "501005"

DEVICE_ID = "pi01"
TOPIC_SENSOR = "nha/pi01/sensor"
TOPIC_STATUS = "nha/pi01/sensor_status"   # riêng, tránh đụng status của camera

INTERVAL = 10                   # giây giữa 2 lần đọc
CSV_DIR = "/home/pi/iot_data"
CSV_PATH = os.path.join(CSV_DIR, "sensors.csv")
CSV_HEADER = ["timestamp", "device_id", "temperature_c", "humidity_pct", "lux"]

DHT_RETRIES = 3                 # số lần thử đọc lại DHT11
DHT_RETRY_DELAY = 2.5           # giây nghỉ giữa 2 lần thử (DHT cần >= 2s)
BH1750_ADDRESS = 0x23           # 0x5C nếu kéo chân ADDR lên cao
# ====================================================


# ---------------- Cảm biến ----------------
# BH1750: board.SCL = GPIO3 (chân 5), board.SDA = GPIO2 (chân 3) -> đúng I2C1
i2c = busio.I2C(board.SCL, board.SDA)
light = adafruit_bh1750.BH1750(i2c, address=BH1750_ADDRESS)

# DHT11: board.D17 = GPIO17 (chân 11).
# use_pulseio phải bật trên Pi; nếu pulseio không có thì thư viện bit-bang
# (chậm, hay lỗi checksum) — in cảnh báo để dễ biết.
dht = adafruit_dht.DHT11(board.D17, use_pulseio=HAS_PULSEIO)
if not HAS_PULSEIO:
    print("CẢNH BÁO: không có pulseio (thiếu libgpiod2/sysv-ipc) — DHT11 sẽ đọc lỗi nhiều")


def read_dht11():
    """Đọc DHT11, thử lại tối đa DHT_RETRIES lần. Trả về (temp, hum) hoặc None."""
    for attempt in range(1, DHT_RETRIES + 1):
        try:
            temp = dht.temperature      # lần đầu thực sự đo
            hum = dht.humidity          # trong 2s -> trả về đúng giá trị vừa đo, miễn phí
            if temp is not None and hum is not None:
                return temp, hum
            print(f"  DHT11 lần {attempt}: đọc được None, thử lại...")
        except RuntimeError as e:
            # "Checksum did not validate", "A full buffer was not returned", ...
            print(f"  DHT11 lần {attempt}: {e}")
        except Exception as e:
            # ví dụ OSError khi pulseio bị đứt -> không được làm chết vòng lặp
            print(f"  DHT11 lần {attempt}: lỗi không mong đợi: {e}")
        if attempt < DHT_RETRIES:
            time.sleep(DHT_RETRY_DELAY)
    return None


def read_bh1750():
    """Đọc cảm biến ánh sáng. Trả về lux hoặc None nếu lỗi I2C."""
    try:
        return float(light.lux)      # .lux luôn là lần đo mới, không cần sleep thêm
    except Exception as e:
        print("Lỗi đọc BH1750:", e)
        return None


# ---------------- CSV trên thẻ nhớ ----------------
def ensure_csv_header():
    """Tạo thư mục + ghi header nếu file chưa có."""
    os.makedirs(CSV_DIR, exist_ok=True)
    if not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0:
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)


def append_csv(row):
    """Thêm 1 dòng vào CSV trên thẻ nhớ."""
    try:
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([row[k] for k in CSV_HEADER])
    except OSError as e:
        print("Lỗi ghi CSV:", e)


# ---------------- MQTT ----------------
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print("Kết nối MQTT thất bại:", reason_code)
        return
    print("Đã kết nối broker")
    client.publish(TOPIC_STATUS, "online", qos=1, retain=True)


def on_disconnect(client, userdata, flags, reason_code, properties):
    print("Mất kết nối MQTT, đang thử lại...")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pi01_sensor")
client.username_pw_set(USER, PASSWORD)
client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.will_set(TOPIC_STATUS, "offline", qos=1, retain=True)
client.reconnect_delay_set(min_delay=1, max_delay=30)
client.connect_async(BROKER, PORT, keepalive=60)
client.loop_start()


# ---------------- Vòng lặp chính ----------------
ensure_csv_header()
next_time = time.monotonic()

try:
    while True:
        dht_vals = read_dht11()
        lux = read_bh1750()

        if dht_vals is None or lux is None:
            # BỎ QUA mẫu này: không ghi dòng rỗng để CSV luôn sạch
            # (không có NaN -> MinMaxScaler/window_transform trong notebook không bị hỏng)
            print(f"[{time.strftime('%H:%M:%S')}] Bỏ qua mẫu (DHT11 hoặc BH1750 lỗi)")
        else:
            temp, hum = dht_vals
            row = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "device_id": DEVICE_ID,
                "temperature_c": round(temp, 1),
                "humidity_pct": round(hum, 1),
                "lux": round(lux, 1),
            }
            append_csv(row)                       # luôn ghi CSV, kể cả khi mất broker
            if client.is_connected():
                client.publish(TOPIC_SENSOR, json.dumps(row), qos=1)
                print(f"[{row['timestamp']}] T={row['temperature_c']}C  "
                      f"H={row['humidity_pct']}%  Lux={row['lux']}")
            else:
                print(f"[{row['timestamp']}] {row} (chưa có broker, chỉ ghi CSV)")

        # Giữ nhịp đều đúng INTERVAL giây
        next_time += INTERVAL
        delay = next_time - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_time = time.monotonic()
except KeyboardInterrupt:
    print("Dừng chương trình")
finally:
    if client.is_connected():
        client.publish(TOPIC_STATUS, "offline", qos=1, retain=True).wait_for_publish(timeout=3)
    try:
        dht.exit()          # dọn tiến trình libgpiod_pulsein
    except Exception:
        pass
    client.loop_stop()
    client.disconnect()
