"""
SERVER chạy trên laptop Windows
- Kết nối broker MQTT (Mosquitto chạy cùng máy)
- Nhận ảnh JPEG từ Pi và hiển thị bằng Streamlit
- Nhận dữ liệu cảm biến (DHT11 + BH1750), lưu CSV và vẽ biểu đồ

Chạy:  streamlit run server_app.py
"""
import collections
import csv
import json
import os
import threading
import time
from datetime import datetime

import matplotlib.pyplot as plt
import pandas as pd
import paho.mqtt.client as mqtt
import streamlit as st

# ===================== CẤU HÌNH =====================
BROKER = "localhost"
PORT = 1883
USER = "pi01"
PASSWORD = "501005"

TOPIC_IMAGE = "nha/+/image"
TOPIC_STATUS = "nha/+/status"
TOPIC_SENSOR = "nha/+/sensor"                # MỚI
TOPIC_SENSOR_STATUS = "nha/+/sensor_status"  # MỚI

SAVE_DIR = r"C:\mqtt\images"
SAVE_SENSOR_DIR = r"C:\mqtt\data"            # MỚI
SENSOR_CSV_PATH = os.path.join(SAVE_SENSOR_DIR, "sensors.csv")
SENSOR_CSV_HEADER = ["timestamp", "device_id", "temperature_c", "humidity_pct", "lux"]
HISTORY_SIZE = 12
SENSOR_HISTORY_SIZE = 360                    # MỚI: 360 mẫu x 10s = 1 giờ
# ====================================================


class Store:
    """Kho dữ liệu dùng chung giữa luồng MQTT và giao diện Streamlit."""

    def __init__(self):
        self.lock = threading.Lock()
        self.latest = None            # bytes JPEG mới nhất
        self.latest_time = None       # datetime nhận ảnh
        self.latest_device = None
        self.count = 0
        self.history = collections.deque(maxlen=HISTORY_SIZE)   # (time, bytes)
        self.pi_status = "chưa rõ"
        self.broker_connected = False
        self.save_to_disk = False
        # --- Dữ liệu cảm biến ---
        self.sensor_latest = None        # dict mẫu mới nhất
        self.sensor_latest_time = None
        self.sensor_device = None
        self.sensor_count = 0
        self.sensor_history = collections.deque(maxlen=SENSOR_HISTORY_SIZE)  # (time, dict)
        self.sensor_status = "chưa rõ"


def valid_sensor_row(row):
    """Kiểm tra mẫu cảm biến có đủ khoá và 3 giá trị đo là số hợp lệ.

    Trả False nếu thiếu khoá -> bỏ qua mẫu, tránh ghi dòng rỗng (NaN) làm
    hỏng MinMaxScaler/window_transform trong notebook.
    """
    if not isinstance(row, dict):
        return False
    for key in ("timestamp", "device_id"):
        if key not in row:
            return False
    for key in ("temperature_c", "humidity_pct", "lux"):
        try:
            float(row[key])
        except (KeyError, TypeError, ValueError):
            return False
    return True


def append_sensor_csv(row):
    """Ghi 1 dòng cảm biến vào CSV (header chỉ ghi lần đầu)."""
    try:
        os.makedirs(SAVE_SENSOR_DIR, exist_ok=True)
        is_new = (not os.path.exists(SENSOR_CSV_PATH)
                  or os.path.getsize(SENSOR_CSV_PATH) == 0)
        with open(SENSOR_CSV_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(SENSOR_CSV_HEADER)
            writer.writerow([row.get(k, "") for k in SENSOR_CSV_HEADER])
    except OSError as e:
        print("Lỗi ghi CSV cảm biến:", e)


@st.cache_resource
def start_mqtt():
    """Chạy 1 lần duy nhất, kể cả khi Streamlit chạy lại script."""
    store = Store()

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            store.broker_connected = False
            return
        store.broker_connected = True
        client.subscribe(TOPIC_IMAGE, qos=1)
        client.subscribe(TOPIC_STATUS, qos=1)
        client.subscribe(TOPIC_SENSOR, qos=1)
        client.subscribe(TOPIC_SENSOR_STATUS, qos=1)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        store.broker_connected = False

    def on_message(client, userdata, msg):
        if msg.topic.endswith("/sensor_status"):
            store.sensor_status = msg.payload.decode(errors="ignore")
            return

        if msg.topic.endswith("/sensor"):
            now = datetime.now()
            device = msg.topic.split("/")[1]
            try:
                row = json.loads(msg.payload.decode())
            except (ValueError, UnicodeDecodeError):
                print("Payload cảm biến không phải JSON:", msg.payload[:80])
                return
            if not valid_sensor_row(row):
                print("Mẫu cảm biến thiếu/tr sai định dạng, bỏ qua:", row)
                return
            with store.lock:
                store.sensor_latest = row
                store.sensor_latest_time = now
                store.sensor_device = device
                store.sensor_count += 1
                store.sensor_history.appendleft((now, row))
            append_sensor_csv(row)
            return

        if msg.topic.endswith("/status"):
            store.pi_status = msg.payload.decode(errors="ignore")
            return

        if msg.topic.endswith("/image"):
            now = datetime.now()
            device = msg.topic.split("/")[1]
            data = bytes(msg.payload)
            with store.lock:
                store.latest = data
                store.latest_time = now
                store.latest_device = device
                store.count += 1
                store.history.appendleft((now, data))
                save = store.save_to_disk
            if save:
                try:
                    os.makedirs(SAVE_DIR, exist_ok=True)
                    name = f"{device}_{now.strftime('%Y%m%d_%H%M%S')}.jpg"
                    with open(os.path.join(SAVE_DIR, name), "wb") as f:
                        f.write(data)
                except OSError as e:
                    print("Lỗi lưu ảnh:", e)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="streamlit_server")
    client.username_pw_set(USER, PASSWORD)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(BROKER, PORT, keepalive=60)
    client.loop_start()
    return store


# ---------------- Giao diện ----------------
st.set_page_config(page_title="Camera + Cảm biến Pi 4", page_icon="📷", layout="wide")
st.title("📷 Camera & Cảm biến Raspberry Pi 4")

store = start_mqtt()

with st.sidebar:
    st.header("Tùy chọn")
    store.save_to_disk = st.checkbox(f"Lưu ảnh vào {SAVE_DIR}", value=store.save_to_disk)
    show_history = st.checkbox("Hiện 12 ảnh gần nhất", value=True)
    st.caption("Giao diện tự làm mới mỗi giây.")


@st.fragment(run_every=1)
def live_view():
    with store.lock:
        latest = store.latest
        latest_time = store.latest_time
        device = store.latest_device
        count = store.count
        history = list(store.history)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Broker", "Đã kết nối" if store.broker_connected else "Mất kết nối")
    c2.metric("Pi", store.pi_status)
    c3.metric("Số ảnh đã nhận", count)
    if latest_time:
        age = int((datetime.now() - latest_time).total_seconds())
        c4.metric("Ảnh cuối cách đây", f"{age} giây")
    else:
        c4.metric("Ảnh cuối cách đây", "-")

    if latest is None:
        st.info("Đang chờ ảnh từ Pi... Hãy chắc chắn pi_camera_client.py đang chạy.")
        return

    st.subheader(f"Ảnh mới nhất ({device}) - {latest_time.strftime('%H:%M:%S %d/%m/%Y')}")
    st.image(latest, use_container_width=True)

    if show_history and len(history) > 1:
        st.subheader("Ảnh gần đây")
        cols = st.columns(4)
        for i, (t, img) in enumerate(history[1:]):
            cols[i % 4].image(img, caption=t.strftime("%H:%M:%S"), use_container_width=True)


live_view()


# ---------------- Giao diện cảm biến ----------------
@st.fragment(run_every=1)
def sensor_view():
    with store.lock:
        latest = store.sensor_latest
        latest_time = store.sensor_latest_time
        device = store.sensor_device
        count = store.sensor_count
        history = list(store.sensor_history)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Cảm biến", store.sensor_status)
    c2.metric("Số mẫu đã nhận", count)
    c3.metric("Nhiệt độ", f"{latest['temperature_c']} °C" if latest else "-")
    c4.metric("Độ ẩm", f"{latest['humidity_pct']} %" if latest else "-")
    c5.metric("Ánh sáng", f"{latest['lux']} lux" if latest else "-")

    if not history:
        st.info("Đang chờ dữ liệu cảm biến từ Pi... "
                "Hãy chắc chắn pi_sensor_client.py đang chạy.")
        return

    st.subheader(f"Dữ liệu cảm biến ({device}) - "
                 f"cập nhật {latest_time.strftime('%H:%M:%S %d/%m/%Y')}")

    # history đang sắp từ mới -> cũ, đảo lại để vẽ đúng chiều thời gian
    rows = [{
        "timestamp": r.get("timestamp", t.isoformat(timespec="seconds")),
        "temperature_c": r.get("temperature_c"),
        "humidity_pct": r.get("humidity_pct"),
        "lux": r.get("lux"),
    } for t, r in history]
    df = pd.DataFrame(rows).iloc[::-1]
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    fig, axes = plt.subplots(3, 1, figsize=(10, 7), sharex=True)
    for ax, col, label, color in [
        (axes[0], "temperature_c", "Nhiệt độ (°C)", "#e4572e"),
        (axes[1], "humidity_pct", "Độ ẩm (%)", "#2e86ab"),
        (axes[2], "lux", "Ánh sáng (lux)", "#8ab03e"),
    ]:
        ax.plot(df["timestamp"], df[col], marker="o", markersize=3,
                linewidth=1.4, color=color)
        ax.set_ylabel(label)
        ax.grid(True, alpha=0.3)
    axes[2].set_xlabel("Thời gian")
    fig.suptitle("Dữ liệu cảm biến DHT11 + BH1750")
    fig.autofmt_xdate()
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)

    st.dataframe(df.tail(10).iloc[::-1], width="stretch")


sensor_view()
