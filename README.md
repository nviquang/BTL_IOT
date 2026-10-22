# BTL_IOT — Chapter 12: Camera + Cảm biến môi trường trên Raspberry Pi 4

Hệ thống IoT thu thập dữ liệu từ **Raspberry Pi 4**, truyền về laptop qua **MQTT**,
hiển thị trực tiếp trên **Streamlit** và lưu ra **CSV** để đưa vào phần time-series
của báo cáo.

```
Raspberry Pi 4                        Laptop Windows
┌──────────────────┐                ┌──────────────────────────────┐
│ USB Webcam ──────┼──(ảnh JPEG)───▶│ Mosquitto (broker) :1883    │
│   ↓ YOLOv8n      │                │        ▲                     │
│   ↓ vẽ khung     │──(JSON det)────┼────────┘                     │
│ DHT11  (chân 11) │                │ server_app.py (Streamlit)    │
│ BH1750 (chân 3,5)│──(JSON 10s)───┼────────┘                     │
└──────────────────┘                │  ├─ ảnh live                 │
                                    │  ├─ metric + 3 biểu đồ      │
                                    │  └─ C:\mqtt\data\sensors.csv │
                                    └──────────────────────────────┘
```

- **Nhiệt độ / độ ẩm**: DHT11 — DATA nối **chân 11** (BCM GPIO17)
- **Ánh sáng**: BH1750 — SDA nối **chân 3** (GPIO2), SCL nối **chân 5** (GPIO3) → bus I2C1
- **Chu kỳ lấy mẫu**: 10 giây
- **Nhận diện người**: YOLOv8n (`model.pt`, class `person`) chạy trên CPU Pi, `imgsz=320`

## 0. Tóm tắt nhanh

**Trên Raspberry Pi:**

```bash
sudo apt install -y libgpiod2 sysv-ipc i2c-tools python3-venv
sudo raspi-config nonint do_i2c 0 && sudo reboot

git clone <URL_GITHUB_CUA_BAN> ~/chapter12
cd ~/chapter12
python3 -m venv venv
source venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
pip install -r requirements-pi.txt
```

**Trên laptop:**

```bash
git clone <URL_GITHUB_CUA_BAN> ~/chapter12
cd ~/chapter12
pip install -r requirements.txt
# chạy broker mosquitto, rồi:
streamlit run server_app.py
```

Chi tiết từng bước: mục 2 (Pi) và mục 3 (laptop).

---

## 1. Yêu cầu phần cứng

| Cảm biến | Chân | → Raspberry Pi 4 | Ghi chú |
|---|---|---|---|
| DHT11 | VCC | chân 1 (3.3V) | dùng 3.3V cho an toàn GPIO |
| DHT11 | OUT / DATA | **chân 11** (GPIO17) | ⚠️ cần điện trỻ 10 kΩ pull-up, xem mục 2 |
| DHT11 | GND | chân 6 | |
| BH1750 | VCC | chân 1 (3.3V) | |
| BH1750 | SDA | **chân 3** (GPIO2) | bus I2C1 |
| BH1750 | SCL | **chân 5** (GPIO3) | bus I2C1 |
| BH1750 | GND | chân 6 | |

> ⚠️ **DHT11 không có điện trở kéo lên bên trong** (DHT22 thì có sẵn ~4.7 kΩ).
> Nếu module breakout không có sẵn điện trở, **phải gắn thêm 10 kΩ giữa DATA và 3.3V**,
> nếu không sẽ lỗi `Checksum did not validate` liên tục.

Cáp số liệu cả 2 cảm biến đều ngắn (≤ 20 cm) — tín hiệu I2C và DHT11 dễ bị nhiễu khi dây dài.

---

## 2. Chuẩn bị Raspberry Pi 4

### 2.1. Cài gói hệ thống

```bash
sudo apt update
sudo apt install -y libgpiod2 sysv-ipc i2c-tools python3-venv
sudo raspi-config nonint do_i2c 0     # bật I2C
sudo reboot
```

`libgpiod2` + `sysv-ipc` là **bắt buộc**: `adafruit_dht` chỉ dùng được đường `pulseio`
(hardware capture) khi có 2 gói này. Thiếu chúng, thư viện tự fallback sang bit-bang →
tỉ lệ lỗi rất cao trên Pi.

`python3-venv` là gói tạo môi trường ảo ở bước 2.3 (mặc định Raspberry Pi OS không
cài sẵn).

### 2.2. Clone mã nguồn từ GitHub

Toàn bộ mã nguồn (script, `requirements-pi.txt`, `model.pt`) nằm trong repo GitHub.
Trên Pi:

```bash
git clone <URL_GITHUB_CUA_BAN> ~/chapter12
cd ~/chapter12
ls
```

Kỳ vọng thấy trong thư mục:

```
model.pt  pi_camera_client.py  pi_detection_client.py  pi_sensor_client.py
requirements-pi.txt  README.md  ...
```

> `model.pt` đã được commit trong repo nên **không cần copy thủ công** — clone về là có.
> Nếu bạn lỡ xoá nó khỏi repo thì copy lại từ laptop:
> `scp model.pt pi@<IP_PI>:~/chapter12/`

### 2.3. Tạo môi trường ảo Python (venv) ngay trong thư mục này

```bash
cd ~/chapter12
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
```

Dấu `(venv)` ở đầu dòng lệnh = môi trường ảo đang hoạt động. Từ giờ mọi lệnh `python`
/`pip` đều trỏ vào `venv/`, không ảnh hưởng hệ thống.

```bash
deactivate          # thoát môi trường ảo
source venv/bin/activate   # kích hoạt lại (mỗi lần mở terminal mới)
```

> Thư mục `venv/` đã nằm trong `.gitignore` nên **không bị push lên GitHub** — mỗi máy
> tự tạo venv riêng, đúng như bạn muốn.

### 2.4. Cài thư viện Python cho Pi

Cài **torch CPU-only trước**, rồi mới cài `requirements-pi.txt` (trong đó có
`ultralytics`). Nếu cài ngược thứ tự, `ultralytics` sẽ kéo bản torch CUDA (~2 GB) —
Pi 4 không có GPU:

```bash
source venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
pip install -r requirements-pi.txt
```

Kiểm tra torch đúng bản CPU:

```bash
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

Kỳ vọng: `2.x.x False`.

### 2.5. Kiểm tra BH1750 trên bus I2C

```bash
i2cdetect -y 1
```

Phải thấy `23` (0x23). Nếu trống → kiểm tra lại dây SDA/SCL, hoặc địa chỉ là `0x5C`
(khi kéo chân ADDR lên cao) → sửa `BH1750_ADDRESS` trong `pi_sensor_client.py`.

### 2.6. Test nhanh 2 cảm biến (không qua MQTT)

```bash
cd ~/chapter12
source venv/bin/activate
python -c "
import board, busio, adafruit_dht, adafruit_bh1750
d = adafruit_dht.DHT11(board.D17)
try:
    print('DHT11:', d.temperature, 'C  /', d.humidity, '%')
except RuntimeError as e:
    print('DHT11 LOI:', e)
print('Lux :', adafruit_bh1750.BH1750(busio.I2C(board.SCL, board.SDA)).lux)
"
```

Kỳ vọng: nhiệt độ 20–35 °C, độ ẩm 30–90 %, lux > 0.
**Chạy lệnh này 5–10 lần liên tiếp** để đo tỉ lệ lỗi — đây là cách duy nhất biết DHT11
đọc ổn trước khi chạy dài. Nếu luôn lỗi checksum → kiểm tra điện trở 10 kΩ pull-up.

### 2.7. Kiểm tra model nhận diện (cho `pi_detection_client.py`)

```bash
cd ~/chapter12
source venv/bin/activate
python -c "
from ultralytics import YOLO
m = YOLO('model.pt')
print(m.names, m.task)
"
```

Kỳ vọng: `{0: 'person'} detect`.

> **Dung lượng:** torch CPU-only + ultralytics ~ 500 MB, `model.pt` 6 MB.
> Pi 4 (4 GB RAM) chạy YOLOv8n ở `imgsz=320` khoảng **1–3 FPS** — đủ để demo.
> Nếu Pi hết RAM, xem phương án ONNX ở mục 2.8.

### 2.8. Phương án thay thế: ONNX (khi Pi hết RAM)

`torch` nặng và ngốn RAM. Cách nhẹ hơn: export model sang ONNX **trên laptop**,
rồi chạy `onnxruntime` trên Pi (không cần torch, không cần ultralytics):

```bash
# trên laptop (trong thư mục có model.pt)
pip install ultralytics onnx onnxruntime
yolo export model=model.pt format=onnx imgsz=320
```

Kết quả: `model.onnx` (~6 MB) — copy sang Pi cùng với `.py`.

```bash
# trên Pi
cd ~/chapter12
source venv/bin/activate
pip install onnxruntime opencv-python
```

Trong `pi_detection_client.py`, thay phần dùng `ultralytics` bằng
`onnxruntime.InferenceSession` + `YOLOv8` post-processing (NMS). Việc này giảm
RAM khoảng 3–4 lần và tăng FPS gấp 2–3 lần, đổi lại phải tự viết NMS.

---

## 3. Chuẩn bị laptop

### 3.1. Clone repo + cài thư viện Python

Laptop cũng chạy từ cùng một repo (để `server_app.py` và notebook nằm cùng chỗ):

```bash
git clone <URL_GITHUB_CUA_BAN> ~/chapter12
cd ~/chapter12
python -m venv venv            # tuỳ chọn: dùng venv riêng cho laptop
source venv/Scripts/activate   # Windows Git Bash: venv/Scripts/activate
pip install -r requirements.txt
```

> Trên Windows, `venv/Scripts/activate` (PowerShell: `.\venv\Scripts\Activate.ps1`).
> Nếu không dùng venv thì `pip install -r requirements.txt` bằng Python hệ thống cũng được.

### 3.2. Broker MQTT (Mosquitto)

Broker chạy trên chính laptop, cổng `1883`, tài khoản `pi01` / `501005`.
Kiểm tra broker đã chạy chưa:

```bash
mosquitto_sub -h localhost -u pi01 -P 501005 -t 'nha/#' -v
```

Nếu chưa cài Mosquitto trên Windows: tải bản Windows từ
<https://mosquitto.org/downloads/> và chạy `mosquitto.exe -v` (hoặc chạy như service).

### 3.3. Sửa địa chỉ broker trong `pi_sensor_client.py`

Sau khi clone về Pi, mở file trên Pi và sửa dòng `BROKER`:

```python
BROKER = "10.208.20.254"   # ← IP laptop, chạy `ipconfig` để xác nhận
```

`server_app.py` dùng `BROKER = "localhost"` (vì nó chạy cùng máy broker).
Cả hai phải trỏ về **cùng một broker**.

> ⚠️ IP laptop có thể đổi mỗi lần đổi mạng. Sửa trực tiếp trên Pi rồi **đừng commit
> thay đổi này** (tránh đẩy IP của máy lên GitHub cho cả nhóm). Nếu muốn cấu hình
> tập trung, đổi `BROKER` thành đọc từ biến môi trường.

---

## 4. Chạy hệ thống

Cần **3 terminal**. Trên Pi, mỗi terminal mới đều phải `cd ~/chapter12` và
`source venv/bin/activate` trước (trừ khi bạn dùng đường dẫn tương đối `venv/bin/python`).

### Terminal 1 — trên Pi: camera *(tuỳ chọn)*
```bash
cd ~/chapter12
source venv/bin/activate
python pi_camera_client.py
```

### Terminal 1' — trên Pi: nhận diện người *(thay cho Terminal 1)*
```bash
cd ~/chapter12
source venv/bin/activate
python pi_detection_client.py
```

Console sẽ in mỗi lần gửi:
```
[2026-10-21T18:35:22] 2 người | 531 ms | 0.7 FPS | ảnh 46.6 KB
```

> ⚠️ `pi_detection_client.py` và `pi_camera_client.py` **cùng mở `/dev/video0`** →
> không chạy đồng thời được. Chọn một trong hai.
> `pi_detection_client.py` làm thêm: vẽ khung người lên ảnh + gửi JSON kết quả
> trên topic `nha/pi01/detection` (dashboard vẫn hiện ảnh bình thường).

### Terminal 2 — trên Pi: cảm biến
```bash
cd ~/chapter12
source venv/bin/activate
python pi_sensor_client.py
```

Console sẽ in mỗi 10 giây:
```
[2026-10-21T18:35:22] T=26.5C  H=58.0%  Lux=312.5
```

### Terminal 3 — trên laptop: dashboard
```bash
cd ~/chapter12
source venv/Scripts/activate   # Windows
streamlit run server_app.py
```

Mở trình duyệt → địa chỉ Streamlit hiện ra (mặc định `http://localhost:8501`).

> Camera (hoặc nhận diện) và cảm biến chạy **song song** được, không xung đột: hai
> `client_id` MQTT khác nhau (`pi01_camera` / `pi01_sensor`) và hai topic trạng thái
> tách riêng (`nha/pi01/status` / `nha/pi01/sensor_status`).
> Client nhận diện dùng `client_id` riêng là `pi01_detection`.

---

## 5. Dữ liệu sinh ra

### 5.1. Trên Pi (thẻ nhớ)

```
/home/pi/iot_data/sensors.csv
```

### 5.2. Trên laptop

```
C:\mqtt\data\sensors.csv
```

Cả hai file dùng **cùng một định dạng**: một file append theo thời gian, header ghi
đúng 1 lần, 5 cột cố định.

```csv
timestamp,device_id,temperature_c,humidity_pct,lux
2026-10-21T09:00:00,pi01,26.5,58.0,312.5
2026-10-21T09:00:10,pi01,26.6,57.9,318.2
2026-10-21T09:00:20,pi01,26.4,58.1,305.7
```

Ở 10 giây/lần → ~17.280 dòng/ngày (~1 MB).

> **Mẫu lỗi bị BỎ QUA, không ghi dòng rỗng.** Nếu DHT11 hoặc BH1750 đọc lỗi, cả
> vòng lấy mẫu đó không ghi gì — CSV luôn sạch, không có NaN (NaN sẽ làm hỏng
> `MinMaxScaler` và `window_transform` trong notebook). Khoảng trống trong timeline
> chỉ là 1 khoảng cách timestamp lớn hơn bình thường.

> **Hai nguồn độc lập.** CSV trên thẻ nhớ ghi *luôn*, MQTT chỉ publish khi
> `is_connected()`. Mất broker → CSV trên Pi vẫn đầy; mất thẻ nhớ → MQTT vẫn nhận.

### 5.3. Topic MQTT

| Topic | Chiều | Nội dung |
|---|---|---|
| `nha/pi01/image` | Pi → broker | ảnh JPEG (camera) |
| `nha/pi01/sensor` | Pi → broker | JSON cảm biến, mỗi 10s |
| `nha/pi01/detection` | Pi → broker | JSON kết quả nhận diện người |
| `nha/pi01/status` | Pi → broker | `online`/`offline` (camera, retained) |
| `nha/pi01/sensor_status` | Pi → broker | `online`/`offline` (cảm biến, retained) |

Payload cảm biến:
```json
{"timestamp": "2026-10-21T09:00:00", "device_id": "pi01",
 "temperature_c": 26.5, "humidity_pct": 58.0, "lux": 312.5}
```

Payload nhận diện (`nha/pi01/detection`):
```json
{"timestamp": "2026-10-21T09:00:00", "device_id": "pi01", "count": 2,
 "boxes": [{"x1": 84, "y1": 63, "x2": 147, "y2": 261, "conf": 0.755}],
 "inference_ms": 531.0, "fps": 0.74, "imgsz": 320, "model": "yolov8n"}
```

Xem JSON thô trên laptop:
```bash
mosquitto_sub -h localhost -u pi01 -P 501005 -t 'nha/+/sensor' -v
mosquitto_sub -h localhost -u pi01 -P 501005 -t 'nha/+/detection' -v
```

---

## 6. Nối dữ liệu vào notebook (Chapter 12 — time series)

Mở `time_series_data_preprocessing.ipynb`, **thay cell tải dữ liệu Yahoo Finance**
(cell 1: `DataReader("AAPL", "yahoo", ...)`) bằng:

```python
df = pd.read_csv(r"C:\mqtt\data\sensors.csv", parse_dates=["timestamp"])
df = df.set_index("timestamp")
close = df["temperature_c"]        # hoặc df["lux"]
```

Phần còn lại của notebook (`rolling(window=20).mean()`, `diff()`, autocorrelation,
`normalize()` bằng `MinMaxScaler`, `window_transform()`) **chạy được nguyên vẹn** vì
`normalize()` cần một `pandas.Series` và index phải là `DatetimeIndex`.

Muốn làm cho cả 2 chuỗi (nhiệt độ + lux) thì gọi `normalize()` riêng cho từng cột.

---

## 7. Xử lý sự cố

| Triệu chứng | Nguyên nhân | Cách sửa |
|---|---|---|
| `DHT11 LOI: Checksum did not validate` | thiếu điện trở 10 kΩ pull-up, hoặc dây quá dài | gắn điện trở 10 kΩ DATA–3.3V, rút ngắn dây |
| `DHT11 LOI` xuyên suốt | thiếu `libgpiod2` / `sysv-ipc` → đang dùng bit-bang | `sudo apt install -y libgpiod2 sysv-ipc` rồi reboot |
| `CẢNH BÁO: không có pulseio` | chưa cài `libgpiod2`/`sysv-ipc` | cài 2 gói trên rồi reboot |
| `i2cdetect -y 1` không thấy `23` | sai dây SDA/SCL, hoặc địa chỉ là `0x5C` | kiểm tra dây; đổi `BH1750_ADDRESS = 0x5C` |
| `Lỗi đọc BH1750: ...` | I2C lỗi tạm thời | code đã tự bỏ qua mẫu và thử lại vòng sau |
| `Bỏ qua mẫu` lặp lại liên tục | cảm biến lỗi kéo dài | xem mục 2.6 — chạy test cảm biến 5–10 lần |
| Dashboard không nhận dữ liệu | sai IP broker trong `pi_sensor_client.py` | `ipconfig` trên laptop rồi sửa `BROKER` |
| Metric "Cảm biến" hiện `chưa rõ` | chưa có retained status | client chưa kết nối broker → kiểm tra mạng |
| Laptop ghi CSV nhiều hơn Pi | đang ghi trùng | kiểm tra `client_id` và topic trong code |
| Ảnh không lên | sai `CAM_INDEX` | thử 1, 2... trong `pi_camera_client.py` |
| `pi_detection_client.py` báo "KHÔNG TÌM THẤY model" | clone về chưa có `model.pt` | `scp model.pt pi@<IP_PI>:~/chapter12/` (xem mục 2.2) |
| `ImportError: libopenblas` / `ImportError: libc10` khi import torch | cài thiếu `libopenblas` | `sudo apt install -y libopenblas-dev libopenblas0` |
| `pip install ultralytics` tải 2 GB | kéo bản torch CUDA | cài torch CPU-only trước (mục 2.4) |
| Pi bị OOM khi chạy detection | RAM 4 GB không đủ cho torch | export ONNX + `onnxruntime` (mục 2.8), hoặc giảm `IMGSZ` xuống 256 |
| `ModuleNotFoundError` khi chạy script | chưa kích hoạt venv | `source venv/bin/activate` (mục 2.3) |
| `git clone` lỗi `Permission denied` | clone vào nhầm chỗ hệ thống | clone về `~/chapter12` (nhà của user `pi`) |
| Hai client camera chạy cùng lúc, ảnh đứng | tranh `/dev/video0` | chỉ chạy một trong `pi_camera_client.py` / `pi_detection_client.py` |
| Dashboard không hiện ảnh có khung | đang chạy `pi_camera_client.py` | chạy `pi_detection_client.py` (ảnh đã vẽ khung) |

---

## 8. Cấu trúc thư mục

| File | Nơi chạy | Vai trò |
|---|---|---|
| `pi_sensor_client.py` | Raspberry Pi | đọc DHT11 + BH1750, ghi CSV trên thẻ nhớ, publish MQTT |
| `pi_camera_client.py` | Raspberry Pi | chụp ảnh webcam, publish MQTT |
| `pi_detection_client.py` | Raspberry Pi | chụp ảnh → YOLOv8n dò người → vẽ khung → publish ảnh + JSON |
| `server_app.py` | Laptop | nhận ảnh + cảm biến, lưu CSV, dashboard Streamlit |
| `requirements-pi.txt` | Raspberry Pi | thư viện cài trong `~/chapter12/venv` |
| `requirements.txt` | Laptop | thư viện cho toàn bộ notebook + server |
| `model.pt` | Laptop + Pi (đã có trong repo) | checkpoint YOLOv8n đã huấn luyện (ultralytics 8.0.166, task `detect`, 1 class: `person`) |
| `time_series_data_preprocessing.ipynb` | Laptop | notebook Chapter 12 — nối với CSV cảm biến (mục 6) |

Các notebook còn lại (`Video_to_frames`, `data_augmentation`, `audio_processing`,
`text_preprocessing`) chạy độc lập với phần cảm biến.

### Cấu trúc sau khi clone trên Pi

```
~/chapter12/
├── venv/                 ← môi trường ảo (tự tạo, không push lên GitHub)
├── model.pt
├── pi_camera_client.py
├── pi_detection_client.py
├── pi_sensor_client.py
├── requirements-pi.txt
├── requirements.txt
├── server_app.py
└── README.md
```

> Nếu muốn repo gọn hơn, có thể bỏ các file không dùng trên Pi (`*.ipynb`, `video.avi`,
> `*.png`) khỏi repo — nhưng nhớ chúng vẫn cần trên laptop cho phần notebook.

---

## 9. Về nội dung Chapter 12

Now that we have understood and implemented different Artificial Intelligence (AI)/machine learning (ML) algorithms, it is time to combine it all together, understand which type of data is best suited for each and at the same time understand the basic pre-processing required for each type of data. By the end of this chapter you will:

* Know the different types of data that can be fed to your model
* Learn how to process time series data
* Pre-processing of textual data
* Different transforms that can be done on image data
* How to handle video files
* How to handle speech data
* Cloud computing options
