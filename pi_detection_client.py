"""
CLIENT nhận diện người chạy trên Raspberry Pi 4
- Mở webcam USB bằng OpenCV, chạy YOLOv8n (model.pt, class "person") trên CPU
- Vẽ khung người lên ảnh, mỗi SEND_INTERVAL giây gửi ảnh JPEG + JSON kết quả về broker MQTT

Thay thế pi_camera_client.py (cùng dùng /dev/video0 nên không chạy song song được).

Chạy: ~/mqtt-env/bin/python ~/pi_detection_client.py
"""
import json
import os
import threading
import time
from datetime import datetime

import cv2
import paho.mqtt.client as mqtt
from ultralytics import YOLO

# ===================== CẦU HÌNH =====================
BROKER = "10.208.20.254"        # IP laptop (chạy ipconfig để kiểm tra)
PORT = 1883
USER = "pi01"
PASSWORD = "501005"

DEVICE_ID = "pi01"
TOPIC_IMAGE = "nha/pi01/image"           # ảnh JPEG đã vẽ khung -> dashboard hiện tại
TOPIC_DETECTION = "nha/pi01/detection"   # JSON kết quả nhận diện
TOPIC_STATUS = "nha/pi01/status"         # online/offline (retained)

CAM_INDEX = 0                   # /dev/video0. Nếu không lên hình, thử 1, 2...
WIDTH, HEIGHT = 640, 480

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model.pt")
IMGSZ = 320                     # 320 nhanh hơn ~2 lần so với 640 trên CPU Pi 4
CONF = 0.25
JPEG_QUALITY = 80               # 1-100, cao hơn = nét hơn nhưng nặng hơn
SEND_INTERVAL = 2               # giây giữa 2 lần gửi ảnh (suy luận vẫn chạy liên tục)
CAM_RETRY_DELAY = 5             # giây chờ trước khi mở lại camera
# ====================================================


# ---------------- Camera ----------------
def open_camera():
    cap = cv2.VideoCapture(CAM_INDEX, cv2.CAP_V4L2)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


class FrameReader(threading.Thread):
    """Đọc camera liên tục ở thread riêng, giữ frame mới nhất.

    YOLO trên Pi chậm hơn FPS camera, nếu đọc frame trực tiếp trong vòng lặp
    chính thì buffer camera sẽ đầy và ảnh bị trễ. Thread này luôn lấy frame
    mới nhất (frame cũ bị ghi đè) để ảnh gửi đi luôn tươi.
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.frame = None
        self.running = True

    def run(self):
        cap = None
        while self.running:
            if cap is None:
                cap = open_camera()
                if cap is None:
                    print("Không mở được camera, thử lại sau %d giây..." % CAM_RETRY_DELAY)
                    time.sleep(CAM_RETRY_DELAY)
                    continue
                print("Đã mở camera")

            ok, frame = cap.read()
            if not ok:
                print("Đọc ảnh lỗi, mở lại camera...")
                cap.release()
                cap = None
                time.sleep(2)
                continue

            with self.lock:
                self.frame = frame

        if cap is not None:
            cap.release()

    def latest(self):
        with self.lock:
            return self.frame

    def stop(self):
        self.running = False


# ---------------- Nhận diện ----------------
def draw_boxes(frame, boxes, conf_threshold=CONF):
    """Vẽ khung người + nhãn lên frame (trả về frame mới, không sửa frame gốc)."""
    out = frame.copy()
    h, w = out.shape[:2]
    for b in boxes:
        x1, y1, x2, y2 = b["x1"], b["y1"], b["x2"], b["y2"]
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 0), 2)
        label = "person %.2f" % b["conf"]
        # nhãn nằm trong khung, không tràn ra ngoài mép trên
        ty = y1 - 6 if y1 - 6 > 12 else y1 + 18
        cv2.putText(out, label, (x1, ty), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (0, 255, 0), 1, cv2.LINE_AA)
    return out


def detect(model, frame):
    """Chạy YOLO. Trả về (boxes, inference_ms) với boxes là list dict."""
    t0 = time.monotonic()
    result = model.predict(frame, imgsz=IMGSZ, conf=CONF, verbose=False)[0]
    inference_ms = (time.monotonic() - t0) * 1000.0

    boxes = []
    if result.boxes is not None:
        for xyxy, conf in zip(result.boxes.xyxy.tolist(), result.boxes.conf.tolist()):
            if conf < CONF:
                continue
            x1, y1, x2, y2 = [int(v) for v in xyxy]
            boxes.append({
                "x1": max(0, x1), "y1": max(0, y1),
                "x2": max(0, x2), "y2": max(0, y2),
                "conf": round(float(conf), 3),
            })
    return boxes, inference_ms


# ---------------- MQTT ----------------
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print("Kết nối MQTT thất bại:", reason_code)
        return
    print("Đã kết nối broker")
    client.publish(TOPIC_STATUS, "online", qos=1, retain=True)


def on_disconnect(client, userdata, flags, reason_code, properties):
    print("Mất kết nối MQTT, đang thử lại...")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pi01_detection")
client.username_pw_set(USER, PASSWORD)
client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.will_set(TOPIC_STATUS, "offline", qos=1, retain=True)
client.reconnect_delay_set(min_delay=1, max_delay=30)
client.connect_async(BROKER, PORT, keepalive=60)
client.loop_start()


# ---------------- Vòng lặp chính ----------------
if not os.path.exists(MODEL_PATH):
    print("KHÔNG TÌM THẤY model tại:", MODEL_PATH)
    print("Hãy copy model.pt sang cùng thư mục với file này trên Pi.")
    raise SystemExit(1)

print("Đang nạp model:", MODEL_PATH)
model = YOLO(MODEL_PATH)
model.to("cpu")
model.fuse()
print("Model đã sẵn sàng (imgsz=%d, conf=%.2f)" % (IMGSZ, CONF))

reader = FrameReader()
reader.start()

last_send = 0.0
last_inference_ms = 0.0
fps = 0.0
prev_time = time.monotonic()

try:
    while True:
        frame = reader.latest()
        if frame is None:
            time.sleep(0.2)
            continue

        boxes, inference_ms = detect(model, frame)
        last_inference_ms = inference_ms

        # FPS suy luận = số lần detect / giây
        now = time.monotonic()
        elapsed = now - prev_time
        if elapsed >= 1.0:
            fps = 1.0 / elapsed
            prev_time = now

        # Chỉ gửi ảnh mỗi SEND_INTERVAL giây (suy luận vẫn chạy liên tục)
        if now - last_send >= SEND_INTERVAL:
            last_send = now
            stamped = frame.copy()
            stamp = datetime.now().isoformat(timespec="seconds")
            cv2.putText(stamped, stamp, (10, 25), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (0, 255, 0), 2, cv2.LINE_AA)
            annotated = draw_boxes(stamped, boxes)
            ok, buf = cv2.imencode(".jpg", annotated,
                                   [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if not ok:
                print("Encode JPEG lỗi, bỏ qua lần gửi này")
                continue

            payload = {
                "timestamp": stamp,
                "device_id": DEVICE_ID,
                "count": len(boxes),
                "boxes": boxes,
                "inference_ms": round(last_inference_ms, 1),
                "fps": round(fps, 2),
                "imgsz": IMGSZ,
                "model": "yolov8n",
            }

            if client.is_connected():
                client.publish(TOPIC_IMAGE, buf.tobytes(), qos=1)
                client.publish(TOPIC_DETECTION, json.dumps(payload), qos=1)
                print("[%s] %d người | %.0f ms | %.1f FPS | ảnh %.1f KB"
                      % (stamp, len(boxes), last_inference_ms, fps,
                         len(buf) / 1024))
            else:
                print("[%s] %d người (chưa có broker, bỏ qua gửi)"
                      % (stamp, len(boxes)))

        time.sleep(0.05)   # tránh vòng lặp quá nóng, không làm nghẽn camera
except KeyboardInterrupt:
    print("\nDừng chương trình")
finally:
    reader.stop()
    if client.is_connected():
        client.publish(TOPIC_STATUS, "offline", qos=1, retain=True).wait_for_publish(timeout=3)
    client.loop_stop()
    client.disconnect()
