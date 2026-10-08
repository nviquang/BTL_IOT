"""
CLIENT chạy trên Raspberry Pi 4
- Mở webcam USB bằng OpenCV
- Mỗi INTERVAL giây chụp 1 ảnh, nén JPEG, gửi về broker MQTT trên laptop

Chạy: ~/mqtt-env/bin/python ~/pi_camera_client.py
"""
import time
import cv2
import paho.mqtt.client as mqtt

# ===================== CẤU HÌNH =====================
BROKER = "10.208.20.254"        # IP laptop (chạy ipconfig để kiểm tra)
PORT = 1883
USER = "pi01"
PASSWORD = "501005"

TOPIC_IMAGE = "nha/pi01/image"
TOPIC_STATUS = "nha/pi01/status"

CAM_INDEX = 0                   # /dev/video0. Nếu không lên hình, thử 1, 2...
WIDTH, HEIGHT = 640, 480
JPEG_QUALITY = 80               # 1-100, cao hơn = nét hơn nhưng nặng hơn
INTERVAL = 5                    # giây giữa 2 ảnh
WARMUP_FRAMES = 5               # bỏ vài frame đầu mỗi lần chụp để cam lấy sáng và xóa buffer cũ
# ====================================================


def open_camera():
    cap = cv2.VideoCapture(CAM_INDEX, cv2.CAP_V4L2)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def capture_jpeg(cap):
    """Chụp 1 ảnh, trả về bytes JPEG hoặc None nếu lỗi."""
    frame = None
    for _ in range(WARMUP_FRAMES):
        ok, frame = cap.read()
        if not ok:
            return None
    # Ghi giờ lên ảnh để dễ kiểm tra
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    cv2.putText(frame, stamp, (10, 25), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (0, 255, 0), 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buf.tobytes() if ok else None


# ---------------- MQTT ----------------
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print("Kết nối MQTT thất bại:", reason_code)
        return
    print("Đã kết nối broker")
    client.publish(TOPIC_STATUS, "online", qos=1, retain=True)


def on_disconnect(client, userdata, flags, reason_code, properties):
    print("Mất kết nối MQTT, đang thử lại...")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pi01_camera")
client.username_pw_set(USER, PASSWORD)
client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.will_set(TOPIC_STATUS, "offline", qos=1, retain=True)
client.reconnect_delay_set(min_delay=1, max_delay=30)
client.connect_async(BROKER, PORT, keepalive=60)
client.loop_start()


# ---------------- Vòng lặp chính ----------------
cap = None
next_time = time.monotonic()

try:
    while True:
        # Mở (hoặc mở lại) camera nếu cần
        if cap is None:
            cap = open_camera()
            if cap is None:
                print("Không mở được camera, thử lại sau 5 giây...")
                time.sleep(5)
                continue
            print("Đã mở camera")

        jpeg = capture_jpeg(cap)
        if jpeg is None:
            print("Đọc ảnh lỗi, mở lại camera...")
            cap.release()
            cap = None
            time.sleep(2)
            continue

        if client.is_connected():
            client.publish(TOPIC_IMAGE, jpeg, qos=1)
            print(f"[{time.strftime('%H:%M:%S')}] Đã gửi ảnh {len(jpeg) / 1024:.1f} KB")
        else:
            print("Chưa kết nối broker, bỏ qua ảnh này")

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
    if cap is not None:
        cap.release()
    if client.is_connected():
        client.publish(TOPIC_STATUS, "offline", qos=1, retain=True).wait_for_publish(timeout=3)
    client.loop_stop()
    client.disconnect()
