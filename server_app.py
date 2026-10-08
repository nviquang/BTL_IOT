"""
SERVER chạy trên laptop Windows
- Kết nối broker MQTT (Mosquitto chạy cùng máy)
- Nhận ảnh JPEG từ Pi và hiển thị bằng Streamlit

Chạy:  streamlit run server_app.py
"""
import collections
import os
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt
import streamlit as st

# ===================== CẤU HÌNH =====================
BROKER = "localhost"
PORT = 1883
USER = "pi01"
PASSWORD = "501005"

TOPIC_IMAGE = "nha/+/image"
TOPIC_STATUS = "nha/+/status"

SAVE_DIR = r"C:\mqtt\images"
HISTORY_SIZE = 12
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

    def on_disconnect(client, userdata, flags, reason_code, properties):
        store.broker_connected = False

    def on_message(client, userdata, msg):
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
st.set_page_config(page_title="Camera Pi 4", page_icon="📷", layout="wide")
st.title("📷 Camera Raspberry Pi 4")

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
