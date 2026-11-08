"""
SERVER chạy trên laptop Windows: dashboard bộ cảm biến của trạm không khí
- Kết nối broker MQTT (Mosquitto chạy cùng máy)
- Nhận mẫu từ collector trên Pi (topic nha/+/sensor): DHT11, BH1750, PMS7003, MQ135 DO
  và trạng thái quạt lọc (controller ghi trong SQLite, collector gửi kèm)
- Lưu CSV định dạng cũ và vẽ biểu đồ

Chạy:  streamlit run server_app.py
"""
import collections
import csv
import json
import os
import sys
import threading
import types
import uuid
from datetime import datetime

import matplotlib.pyplot as plt
import pandas as pd
import paho.mqtt.client as mqtt
import streamlit as st

from aiot.aqi import classify
from aiot.config import load_config
from aiot.payload import valid_sensor_row  # dùng chung với collector trên Pi

# ===================== CẤU HÌNH =====================
# Mosquitto chạy cùng máy nên broker là localhost. User và cổng lấy từ config.yaml,
# mật khẩu là MQTT_PASSWORD trong .env (cùng thư mục với file này)
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = load_config(os.path.join(HERE, "config.yaml"), os.path.join(HERE, ".env"))
BROKER = "localhost"
PORT = CFG.mqtt.port
USER = CFG.mqtt.user
PASSWORD = CFG.secrets.mqtt_password
if PASSWORD is None:
    print("CẢNH BÁO: chưa có MQTT_PASSWORD trong .env, broker sẽ từ chối kết nối")

TOPIC_SENSOR = "nha/+/sensor"
TOPIC_SENSOR_STATUS = "nha/+/sensor_status"

SAVE_SENSOR_DIR = r"C:\mqtt\data"
SENSOR_CSV_PATH = os.path.join(SAVE_SENSOR_DIR, "sensors.csv")
SENSOR_CSV_HEADER = ["timestamp", "device_id", "temperature_c", "humidity_pct", "lux"]
SENSOR_HISTORY_SIZE = 360                    # 360 mẫu x 10s = 1 giờ
STALE_AFTER_S = 60                           # quá 60 s không có mẫu mới thì cảnh báo
# ====================================================


class Store:
    """Kho dữ liệu dùng chung giữa luồng MQTT và giao diện Streamlit."""

    def __init__(self):
        self.lock = threading.Lock()
        self.broker_connected = False
        self.sensor_latest = None        # dict mẫu mới nhất
        self.sensor_latest_time = None
        self.sensor_device = None
        self.sensor_count = 0
        self.sensor_history = collections.deque(maxlen=SENSOR_HISTORY_SIZE)  # (time, dict)
        self.sensor_status = "chưa rõ"


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


def get_store():
    """Store + client MQTT duy nhất cho cả process, kể cả khi Streamlit chạy lại script.

    Không dùng st.cache_resource: cache đó tạo bản mới mỗi khi mã hàm đổi (sửa file lúc
    dashboard đang chạy) mà không dừng client cũ, nên có hai client song song; trước đây
    chúng cùng client_id và broker đá nhau liên tục ("Mất kết nối" nhấp nháy). Giữ trong
    một module sống suốt process thì chỉ có một client; sửa file thì khởi động lại dashboard
    để áp dụng mã mới."""
    registry = sys.modules.setdefault("_aiot_dashboard", types.ModuleType("_aiot_dashboard"))
    lock = registry.__dict__.setdefault("lock", threading.Lock())   # setdefault nguyên tử (GIL)
    with lock:
        if getattr(registry, "store", None) is None:
            registry.store = start_mqtt()
        return registry.store


def start_mqtt():
    """Tạo Store và client MQTT chạy nền; chỉ gọi qua get_store()."""
    store = Store()

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            store.broker_connected = False
            return
        store.broker_connected = True
        client.subscribe(TOPIC_SENSOR, qos=1)
        client.subscribe(TOPIC_SENSOR_STATUS, qos=1)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        store.broker_connected = False

    def on_message(client, userdata, msg):
        if msg.topic.endswith("/sensor_status"):
            store.sensor_status = msg.payload.decode(errors="ignore")
            return
        if not msg.topic.endswith("/sensor"):
            return
        now = datetime.now()
        device = msg.topic.split("/")[1]
        try:
            row = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError):
            print("Payload cảm biến không phải JSON:", msg.payload[:80])
            return
        if not valid_sensor_row(row):
            print("Mẫu cảm biến thiếu/sai định dạng, bỏ qua:", row)
            return
        with store.lock:
            store.sensor_latest = row
            store.sensor_latest_time = now
            store.sensor_device = device
            store.sensor_count += 1
            store.sensor_history.appendleft((now, row))
        append_sensor_csv(row)

    # client_id riêng mỗi process: hai dashboard cùng client_id thì broker đá nhau liên tục
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"streamlit_server_{os.getpid()}_{uuid.uuid4().hex[:6]}")
    client.username_pw_set(USER, PASSWORD)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(BROKER, PORT, keepalive=60)
    client.loop_start()
    return store


# ---------------- Định dạng giá trị ----------------
def fmt(value, unit, digits=1):
    """Số có đơn vị; None (cảm biến lỗi hoặc collector cũ không gửi) thành 'lỗi/không có'."""
    if value is None:
        return "lỗi / không có"
    try:
        return f"{float(value):.{digits}f} {unit}"
    except (TypeError, ValueError):
        return "lỗi / không có"


def aqi_label(pm25):
    try:
        return classify(pm25).label
    except ValueError:
        return None


def mq135_text(do):
    """DO: 0 = đèn DO sáng = vượt ngưỡng; 1 = chưa vượt; None = đang sấy hoặc đọc lỗi."""
    if do is None:
        return "Đang sấy / lỗi", None
    if int(do) == 0:
        return "VƯỢT NGƯỠNG", "Đèn DO sáng (bit = 0)"
    return "Bình thường", "Chưa vượt ngưỡng (bit = 1)"


def fan_text(row):
    """Trạng thái quạt từ các trường fan_* collector gửi kèm."""
    if "fan_on" not in row:
        return "Không rõ", "Collector trên Pi chưa cập nhật"
    on = row.get("fan_on")
    if on is None:
        return "Không rõ", "Controller không chạy"
    mode = "thủ công" if row.get("fan_mode") == "manual" else "tự động"
    state = "BẢO VỆ" if row.get("fan_state") == "PROTECT" else "BÌNH THƯỜNG"
    note = f"{mode}, {state}"
    if row.get("fan_relay_fault"):
        note += ", LỖI RELAY"
    return ("BẬT" if on else "TẮT"), note


# ---------------- Giao diện ----------------
st.set_page_config(page_title="Trạm không khí Pi 4", page_icon="🌫️", layout="wide")
st.title("🌫️ Trạm quan trắc không khí - Raspberry Pi 4")

store = get_store()


@st.fragment(run_every=1)
def sensor_view():
    with store.lock:
        latest = store.sensor_latest
        latest_time = store.sensor_latest_time
        device = store.sensor_device
        count = store.sensor_count
        history = list(store.sensor_history)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Broker", "Đã kết nối" if store.broker_connected else "Mất kết nối")
    c2.metric("Collector", store.sensor_status)
    c3.metric("Số mẫu đã nhận", count)
    age = int((datetime.now() - latest_time).total_seconds()) if latest_time else None
    c4.metric("Mẫu cuối cách đây", f"{age} giây" if age is not None else "-")

    if latest is None:
        st.info("Đang chờ dữ liệu cảm biến từ Pi... "
                "Hãy chắc chắn dịch vụ aiot-collector đang chạy.")
        return
    if age is not None and age > STALE_AFTER_S:
        st.warning(f"Đã {age} giây không nhận được mẫu mới, số liệu bên dưới có thể đã cũ.")

    st.subheader(f"Số liệu hiện tại ({device}) - "
                 f"cập nhật {latest_time.strftime('%H:%M:%S %d/%m/%Y')}")

    # Bụi PMS7003
    pm25 = latest.get("pm25")
    p1, p2, p3 = st.columns(3)
    p1.metric("PM1.0 (PMS7003)", fmt(latest.get("pm1"), "µg/m³", 0))
    p2.metric("PM2.5 (PMS7003)", fmt(pm25, "µg/m³", 0))
    p3.metric("PM10 (PMS7003)", fmt(latest.get("pm10"), "µg/m³", 0))
    label = aqi_label(pm25)
    if label:
        st.caption(f"Nhóm AQI theo PM2.5 tức thời (US EPA 2024): **{label}**")

    # Môi trường DHT11 + BH1750
    e1, e2, e3 = st.columns(3)
    e1.metric("Nhiệt độ (DHT11)", fmt(latest.get("temperature_c"), "°C"))
    e2.metric("Độ ẩm (DHT11)", fmt(latest.get("humidity_pct"), "%"))
    e3.metric("Ánh sáng (BH1750)", fmt(latest.get("lux"), "lux"))

    # MQ135 DO và quạt lọc
    g1, g2 = st.columns(2)
    mq_value, mq_note = mq135_text(latest.get("mq135_do"))
    g1.metric("Khí MQ135 (chân DO)", mq_value, help="Chỉ là 1 bit vượt ngưỡng, không phải nồng độ")
    if mq_note:
        g1.caption(mq_note)
    fan_value, fan_note = fan_text(latest)
    g2.metric("Quạt lọc (relay GPIO23)", fan_value)
    g2.caption(fan_note)

    # Biểu đồ: history sắp từ mới -> cũ, đảo lại để vẽ đúng chiều thời gian
    rows = [{
        "timestamp": r.get("timestamp", t.isoformat(timespec="seconds")),
        "pm1": r.get("pm1"),
        "pm25": r.get("pm25"),
        "pm10": r.get("pm10"),
        "temperature_c": r.get("temperature_c"),
        "humidity_pct": r.get("humidity_pct"),
        "lux": r.get("lux"),
        "mq135_do": r.get("mq135_do"),
        "fan_on": r.get("fan_on"),
    } for t, r in history]
    df = pd.DataFrame(rows).iloc[::-1]
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    num_cols = ["pm1", "pm25", "pm10", "temperature_c", "humidity_pct", "lux"]
    df[num_cols] = df[num_cols].apply(pd.to_numeric, errors="coerce")

    fig, axes = plt.subplots(4, 1, figsize=(10, 9), sharex=True)
    for col, label_, color in [("pm1", "PM1.0", "#9b5de5"), ("pm25", "PM2.5", "#e4572e"),
                               ("pm10", "PM10", "#6c757d")]:
        axes[0].plot(df["timestamp"], df[col], marker="o", markersize=2,
                     linewidth=1.2, color=color, label=label_)
    axes[0].set_ylabel("Bụi (µg/m³)")
    axes[0].legend(loc="upper left", fontsize=8)
    # Tô nền những đoạn quạt đang bật
    fan = df["fan_on"].map(lambda v: v is True)
    if fan.any():
        axes[0].fill_between(df["timestamp"], 0, 1, where=fan, step="post", alpha=0.12,
                             color="#2e86ab", transform=axes[0].get_xaxis_transform(),
                             label="Quạt bật")
        axes[0].legend(loc="upper left", fontsize=8)
    for ax, col, label_, color in [
        (axes[1], "temperature_c", "Nhiệt độ (°C)", "#f4a261"),
        (axes[2], "humidity_pct", "Độ ẩm (%)", "#2e86ab"),
        (axes[3], "lux", "Ánh sáng (lux)", "#8ab03e"),
    ]:
        ax.plot(df["timestamp"], df[col], marker="o", markersize=2,
                linewidth=1.2, color=color)
        ax.set_ylabel(label_)
    for ax in axes:
        ax.grid(True, alpha=0.3)
    axes[3].set_xlabel("Thời gian")
    fig.suptitle("PMS7003 + DHT11 + BH1750 (1 giờ gần nhất)")
    fig.autofmt_xdate()
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)

    table = df.tail(10).iloc[::-1].rename(columns={
        "timestamp": "Thời gian", "pm1": "PM1.0", "pm25": "PM2.5", "pm10": "PM10",
        "temperature_c": "Nhiệt độ", "humidity_pct": "Độ ẩm", "lux": "Lux",
        "mq135_do": "MQ135 DO", "fan_on": "Quạt bật",
    })
    st.dataframe(table, width="stretch")


sensor_view()
