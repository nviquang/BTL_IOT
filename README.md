# BTL_IOT — Raspberry Pi 4 gửi camera + cảm biến về laptop qua MQTT

Hai máy, một broker:

```
Raspberry Pi 4                          Laptop Windows
┌─────────────────────┐                ┌──────────────────────────────┐
│ USB webcam          │                │                              │
│   ↓ YOLOv8n         │──ảnh JPEG─────▶│  Mosquitto broker :1883      │
│   ↓ vẽ khung người  │──JSON detect──▶│        ▲                     │
│ DHT11  (chân 11)    │                │        │                     │
│ BH1750 (chân 3, 5)  │──JSON cảm biến▶│  server_app.py (Streamlit)   │
└─────────────────────┘                │   ├─ ảnh live + 12 ảnh gần   │
                                       │   ├─ 3 biểu đồ cảm biến      │
                                       │   └─ C:\mqtt\data\sensors.csv│
                                       └──────────────────────────────┘
```

- **Nhiệt độ / độ ẩm**: DHT11 — DATA nối **chân 11** (BCM GPIO17), cần điện trở 10 kΩ pull-up
- **Ánh sáng**: BH1750 — SDA **chân 3** (GPIO2), SCL **chân 5** (GPIO3) → bus I2C1
- **Chu kỳ cảm biến**: 10 giây/lần
- **Nhận diện người**: YOLOv8n (`model.pt`, 1 class `person`) chạy trên CPU Pi, `imgsz=320`
- **Chu kỳ ảnh**: 2 giây/lần

Tài liệu chia làm **2 phase**:

| Phase | Làm khi nào | Nội dung |
|---|---|---|
| **1. Khởi tạo** | một lần duy nhất cho mỗi máy | cài gói hệ thống, clone repo, tạo venv, cài thư viện, tạo tài khoản MQTT |
| **2. Chạy** | mỗi buổi demo | bật broker, bật 2 client trên Pi, bật dashboard trên laptop |

---

# PHASE 1 — Khởi tạo (một lần)

## 1A. Trên Raspberry Pi 4

### 1A.1. Gói hệ thống + bật I2C

```bash
sudo apt update
sudo apt install -y libgpiod2 sysv-ipc i2c-tools python3-venv
sudo raspi-config nonint do_i2c 0
sudo reboot
```

`libgpiod2` + `sysv-ipc` là **bắt buộc**: chỉ khi có 2 gói này `adafruit_dht` mới dùng được
đường đọc `pulseio` (hardware capture). Thiếu chúng thư viện tự lùi về bit-bang → lỗi
`Checksum did not validate` liên tục.

### 1A.2. Clone repo

```bash
git clone <URL_GITHUB_CUA_BAN> ~/BTL_IOT
cd ~/BTL_IOT
ls
```

Phải thấy: `model.pt  pi_sensor_client.py  pi_detection_client.py  pi_camera_client.py
requirements-pi.txt  server_app.py  README.md`

`model.pt` đã nằm trong repo nên không cần copy tay.

### 1A.3. Tạo venv

```bash
cd ~/BTL_IOT
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
```

Mỗi terminal mới đều phải `cd ~/BTL_IOT && source venv/bin/activate`. Thư mục `venv/`
đã nằm trong `.gitignore`.

### 1A.4. Cài torch CPU-only **trước**, rồi mới cài requirements

```bash
source venv/bin/activate
pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
pip install -r requirements-pi.txt
```

Thứ tự này quan trọng: nếu để `ultralytics` tự kéo torch, pip sẽ lấy bản CUDA (~2 GB)
trong khi Pi 4 không có GPU.

Kiểm tra:

```bash
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# kỳ vọng: 2.x.x+cpu False
```

> **Bẫy `lgpio`** — nếu `pip install -r requirements-pi.txt` dừng ở bước build `lgpio`
> (`swig failed`, `cannot find -llgpio`, hoặc cài xong mà `import lgpio` vẫn lỗi), xem
> mục **Phụ lục C**.

### 1A.5. Kiểm tra phần cứng

```bash
i2cdetect -y 1          # phải thấy 23 (0x23)
```

Nếu trống → kiểm tra dây SDA/SCL; nếu địa chỉ là `0x5C` (chân ADDR kéo cao) thì sửa
`BH1750_ADDRESS` trong `pi_sensor_client.py`.

Test nhanh 2 cảm biến, chạy 5–10 lần liên tiếp để biết tỉ lệ lỗi thật:

```bash
cd ~/BTL_IOT && source venv/bin/activate
python -c "
import board, busio, adafruit_dht, adafruit_bh1750
d = adafruit_dht.DHT11(board.D17)
try:
    print('DHT11:', d.temperature, 'C /', d.humidity, '%')
except RuntimeError as e:
    print('DHT11 LOI:', e)
print('Lux :', adafruit_bh1750.BH1750(busio.I2C(board.SCL, board.SDA)).lux)
"
```

Kỳ vọng: 20–35 °C, 30–90 %, lux > 0. Thỉnh thoảng một lần lỗi checksum là bình thường —
client sẽ bỏ qua mẫu đó và thử lại vòng sau.

Test model:

```bash
cd ~/BTL_IOT && source venv/bin/activate
python -c "from ultralytics import YOLO; m = YOLO('model.pt'); print(m.names, m.task)"
# kỳ vọng: {0: 'person'} detect
```

## 1B. Trên laptop Windows

### 1B.1. Thư viện Python

```bash
git clone <URL_GITHUB_CUA_BAN> ~/BTL_IOT
cd ~/BTL_IOT
pip install -r requirements.txt
```

`requirements.txt` gồm `paho-mqtt`, `streamlit`, `pandas`, `matplotlib` và thư viện cho
các notebook. Nếu dùng venv riêng cho laptop thì tạo trước rồi mới `pip install`
(`python -m venv venv` → `.\venv\Scripts\Activate.ps1`).

### 1B.2. Cài Mosquitto (broker)

Tải bản Windows từ <https://mosquitto.org/downloads/>, cài vào
`C:\Program Files\Mosquitto` (mặc định).

### 1B.3. Tạo tài khoản MQTT `pi01`

Broker cấu hình `allow_anonymous false`, nên phải có user/password. Cả 3 client dùng
chung tài khoản `pi01`. Mật khẩu tự chọn, ghi vào `MQTT_PASSWORD` trong file `.env`
(chép từ `.env.example`, không commit) trên cả Pi và laptop:

```powershell
& "C:\Program Files\Mosquitto\mosquitto_passwd.exe" -b -c "C:\Users\<TEN_BAN>\mosq\passwd" pi01 <MAT_KHAU_MQTT>
```

- `-c` = **tạo mới** file password (ghi đè). Bỏ `-c` nếu file đã có.
- Đặt file trong thư mục người dùng, **không đặt trong `C:\Program Files`** — xem Phụ lục D.

### 1B.4. Tạo file cấu hình broker

Tạo `C:\Users\<TEN_BAN>\mosq\mosquitto.conf`:

```conf
listener 1883
allow_anonymous false
password_file C:\Users\<TEN_BAN>\mosq\passwd
```

Ba dòng này là đủ. Không cần sửa file trong `C:\Program Files\Mosquitto`.

---

# PHASE 2 — Chạy hệ thống

Thứ tự dưới đây quan trọng: broker phải sống trước khi client kết nối.

## 2.1. Lấy IP LAN của laptop

Trên laptop:

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like '192.168.*' -or $_.IPAddress -like '10.*' } | Select-Object IPAddress, InterfaceAlias
```

Ghi lại IP của card Wi-Fi, ví dụ `10.162.117.254`.

> ⚠️ **IP này đổi mỗi lần đổi mạng.** Sửa trên Pi rồi **đừng commit** thay đổi đó —
> tránh đẩy IP máy bạn lên GitHub cho cả nhóm.

## 2.2. Trỏ client trên Pi về laptop

Mở `config.yaml` trên Pi, sửa `host` trong mục `mqtt`:

```yaml
mqtt:
  host: 10.162.117.254      # ← IP laptop ở bước 2.1
```

`pi_sensor_client.py`, `pi_detection_client.py`, `pi_camera_client.py` đều đọc IP từ đây,
và đọc mật khẩu MQTT từ `MQTT_PASSWORD` trong `.env` (cùng thư mục).

Kiểm tra nhanh Pi có thông tới broker không:

```bash
python3 -c "import socket; s=socket.create_connection(('10.162.117.254',1883),5); print('OK'); s.close()"
```

Nếu lỗi `Connection refused` → broker chưa chạy (2.3).
Nếu `timed out` → firewall Windows chặn, hoặc hai máy khác mạng.

## 2.3. Bật broker trên laptop

```powershell
& "C:\Program Files\Mosquitto\mosquitto.exe" -c "C:\Users\<TEN_BAN>\mosq\mosquitto.conf" -v
```

Log đúng phải có 2 dòng cuối:

```
Opening ipv4 listen socket on port 1883.
mosquitto version 2.1.2 running
```

Giữ terminal này mở suốt buổi demo. Kiểm tra từ máy khác (hoặc terminal khác):

```powershell
& "C:\Program Files\Mosquitto\mosquitto_sub.exe" -h localhost -u pi01 -P <MAT_KHAU_MQTT> -t "nha/#" -v
```

## 2.4. Chạy cảm biến trên Pi

### 2.4a. Dịch vụ collector (thay cho `pi_sensor_client.py`)

> **Trạng thái: có code, chưa triển khai lên Pi.** Các lệnh dưới đây là cách triển khai
> dự kiến; chưa chạy thử trên mạch thật.

`aiot/collector.py` đọc **cả 4 cảm biến** (DHT11, BH1750, PMS7003, MQ135 DO) mỗi 10 giây:

- ghi mẫu thô vào SQLite `raw_readings` (`db_path` trong `config.yaml`), kể cả khi DHT11
  lỗi; gộp trung bình 5 phút vào `readings` mỗi phút (chỉ khi đồng hồ đã đồng bộ NTP);
  xoá mẫu thô cũ hơn 7 ngày mỗi ngày một lần;
- ghi `sensors.csv` **đúng định dạng cũ** (chỉ khi có đủ DHT11 và lux);
- gửi MQTT `nha/pi01/sensor` (payload cũ thêm `pm1`, `pm25`, `pm10`, `mq135_do`) nếu
  `mqtt.enabled` và có `MQTT_PASSWORD`; mất broker thì vẫn thu thập;
- `mq135_do` để trống trong 180 giây đầu vì MQ135 cần sấy.

**Không bao giờ chạy collector và `pi_sensor_client.py` cùng lúc**: hai tiến trình
tranh nhau DHT11, BH1750 và cổng UART. `pi_sensor_client.py` được giữ lại làm bản cũ.

Triển khai (một lần):

```bash
# 0) Đưa code lên Pi. Nhánh feat/aiot-core chưa push lên GitHub, nên chép file
#    từ laptop (Git Bash, ở thư mục repo) bằng scp:
#      scp -r aiot deploy config.yaml requirements-pi.txt admin@<IP_PI>:~/BTL_IOT/
#    Khi nhánh đã push thì dùng: cd ~/BTL_IOT && git pull
#    .env (MQTT_PASSWORD) chép riêng và không commit; thiếu .env thì collector bỏ qua MQTT.

# 1) Múi giờ (CSV ghi giờ địa phương) và kiểm tra đồng bộ NTP
sudo timedatectl set-timezone Asia/Ho_Chi_Minh
timedatectl show -p NTPSynchronized --value     # "yes"; "no" thì mẫu bị gắn clock_synced=0 và chưa được gộp

# 2) Quyền truy cập GPIO, I2C, UART cho user admin (đăng nhập lại sau khi thêm nhóm)
sudo usermod -aG gpio,i2c,dialout admin
ls -l /dev/serial0                              # UART bật, serial console tắt (test_all.py đã đọc PMS7003 ở đây)

# 3) Thư viện (đã có trong requirements-pi.txt; lỗi lgpio xem Phụ lục C)
cd ~/BTL_IOT && source venv/bin/activate
pip install pyserial lgpio PyYAML paho-mqtt adafruit-blinka \
    adafruit-circuitpython-dht adafruit-circuitpython-bh1750

# 4) Tìm và dừng client cũ, dù nó chạy kiểu nào
pgrep -af pi_sensor_client.py                   # tiến trình đang chạy (terminal tay, nohup, tmux, screen)
systemctl list-units --all | grep -i sensor     # nếu là dịch vụ systemd: sudo systemctl disable --now <tên>
crontab -l | grep -i pi_sensor_client           # nếu có dòng @reboot: crontab -e và xoá dòng đó
tmux ls 2>/dev/null                             # nếu có phiên tmux chạy client: tmux kill-session -t <tên>
pkill -f pi_sensor_client.py
pgrep -af pi_sensor_client.py                   # không còn dòng nào

# 5) Chạy thử ở foreground: phải thấy log T=... H=... Lux=... PM2.5=... mỗi 10 giây.
#    Ctrl+C để dừng (dừng sạch, gửi offline lên MQTT).
python -m aiot.collector

# 6) Cài dịch vụ systemd
sudo cp deploy/aiot-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiot-collector
journalctl -u aiot-collector -f
```

Kiểm tra sau khoảng 1 giờ (thiếu lệnh `sqlite3` thì `sudo apt install -y sqlite3`):

```bash
sqlite3 ~/iot_data/aiot.db "select count(*), sum(pm25 is null), max(ts) from raw_readings; select count(*), sum(valid) from readings;"
# kỳ vọng: ~360 mẫu thô, ~12 dòng readings, phần lớn valid=1
tail -3 ~/iot_data/sensors.csv
```

Dừng/khởi động lại: `sudo systemctl stop aiot-collector` / `sudo systemctl restart aiot-collector`.

### 2.4b. Client cũ `pi_sensor_client.py` (chỉ DHT11 + BH1750)

Chỉ dùng khi collector đã dừng (`sudo systemctl stop aiot-collector`).

```bash
cd ~/BTL_IOT && source venv/bin/activate
python pi_sensor_client.py
```

Mỗi 10 giây in một dòng khi gửi thành công:

```
[2026-10-22T22:39:23] T=29.3C  H=76.0%  Lux=12.5
```

Nếu DHT11 lỗi, mẫu đó bị **bỏ qua** (không ghi dòng rỗng) và in `Bỏ qua mẫu (...)`.

## 2.5. Chạy nhận diện trên Pi (terminal 2)

```bash
cd ~/BTL_IOT && source venv/bin/activate
python pi_detection_client.py
```

Log:

```
Đang nạp model: /home/admin/BTL_IOT/model.pt
Đã kết nối broker
Đã mở camera
[2026-10-22T22:40:48] 0 người | 282 ms | 1.0 FPS | ảnh 64.2 KB
```

- `pi_detection_client.py` và `pi_camera_client.py` **cùng mở `/dev/video0`** → chỉ chạy
  một trong hai.
- Cảm biến và nhận diện chạy **song song** được: khác `client_id` (`pi01_sensor` /
  `pi01_detection`) và khác topic trạng thái.

## 2.6. Chạy dashboard trên laptop (terminal 3)

```powershell
cd C:\Users\nviquang\Documents\University\FourthYear\IoT\BTL\Chapter12
python -m streamlit run server_app.py
```

Mở <http://localhost:8501>. Giao diện tự làm mới mỗi giây.

> **Phải mở tab trình duyệt thì dashboard mới thật sự chạy.** Streamlit chỉ thực thi
> `server_app.py` khi có phiên kết nối; để tiến trình chạy mà không mở tab thì nó chưa
> `subscribe` MQTT, và `C:\mqtt\data\sensors.csv` **chưa được ghi**. Đóng tab = dừng
> nhận dữ liệu cho tới khi mở lại.

## 2.7. Nghiệm thu

Dashboard phải hiện:

| Vị trí | Giá trị đúng |
|---|---|
| metric **Broker** | `Đã kết nối` |
| metric **Pi** | `online` |
| metric **Số ảnh đã nhận** | tăng đều ~2 giây/lần |
| metric **Ảnh cuối cách đây** | 0–3 giây |
| metric **Cảm biến** | `online` |
| Nhiệt độ / Độ ẩm / Ánh sáng | số thực, cập nhật 10 giây/lần |
| 3 biểu đồ | có điểm mới sau ~1 phút |

Ba cách kiểm tra độc lập:

```powershell
# 1) mọi topic
& "C:\Program Files\Mosquitto\mosquitto_sub.exe" -h localhost -u pi01 -P <MAT_KHAU_MQTT> -t "nha/#" -v

# 2) chỉ cảm biến (JSON)
& "C:\Program Files\Mosquitto\mosquitto_sub.exe" -h localhost -u pi01 -P <MAT_KHAU_MQTT> -t "nha/+/sensor" -v

# 3) file CSV trên laptop
Get-Content C:\mqtt\data\sensors.csv -Tail 5
```

CSV phải có header đúng một lần và các dòng cùng timestamp với log trên Pi.

---

# Phụ lục A — Topic MQTT

| Topic | Chiều | Nội dung |
|---|---|---|
| `nha/pi01/image` | Pi → broker | ảnh JPEG đã vẽ khung người |
| `nha/pi01/detection` | Pi → broker | JSON kết quả nhận diện |
| `nha/pi01/sensor` | Pi → broker | JSON cảm biến, mỗi 10 s |
| `nha/pi01/status` | Pi → broker | `online` / `offline` (camera, retained) |
| `nha/pi01/sensor_status` | Pi → broker | `online` / `offline` (cảm biến, retained) |

Payload cảm biến:

```json
{"timestamp": "2026-10-22T22:39:23", "device_id": "pi01",
 "temperature_c": 29.3, "humidity_pct": 76, "lux": 12.5}
```

Collector gửi thêm bụi và bit MQ135 (`null` khi đọc lỗi hoặc MQ135 đang sấy);
`server_app.py` vẫn nhận vì chỉ kiểm tra 5 khoá cũ (`aiot/payload.py: valid_sensor_row`):

```json
{"timestamp": "2026-11-05T08:00:00", "device_id": "pi01",
 "temperature_c": 28.5, "humidity_pct": 83.0, "lux": 170.8,
 "pm1": 26.0, "pm25": 39.0, "pm10": 47.0, "mq135_do": 1}
```

Payload nhận diện:

```json
{"timestamp": "2026-10-22T22:40:46", "device_id": "pi01", "count": 0,
 "boxes": [], "inference_ms": 282.0, "fps": 1.0, "imgsz": 320, "model": "yolov8n"}
```

Khi có người trong khung, `count > 0` và `boxes` có phần tử dạng
`{"x1": 84, "y1": 63, "x2": 147, "y2": 261, "conf": 0.755}`.

> **Dashboard hiện không subscribe `nha/+/detection`.** Nó chỉ subscribe `image`,
> `status`, `sensor`, `sensor_status`. Nên JSON nhận diện vẫn được publish và vẫn xem
> được bằng `mosquitto_sub`, nhưng **không hiển thị trên giao diện**. Khung người thì
> vẫn thấy — vì chúng được vẽ vào JPEG trước khi gửi. Muốn hiện `count`/`inference_ms`
> trên dashboard thì phải thêm `client.subscribe("nha/+/detection", qos=1)` vào
> `on_connect` của `server_app.py`.

# Phụ lục B — Dữ liệu sinh ra

Hai file CSV, **cùng định dạng**, ghi độc lập:

| Máy | Đường dẫn |
|---|---|
| Pi | `/home/admin/iot_data/sensors.csv` (`csv_path` trong `config.yaml`) |
| Laptop | `C:\mqtt\data\sensors.csv` |

```csv
timestamp,device_id,temperature_c,humidity_pct,lux
2026-10-22T22:39:23,pi01,29.3,76,12.5
2026-10-22T22:39:33,pi01,28.9,80,12.5
```

- Header ghi đúng 1 lần, 5 cột cố định, append theo thời gian.
- 10 giây/mẫu → ~8.640 dòng/ngày.
- **Mẫu lỗi bị bỏ qua, không ghi dòng rỗng.** CSV luôn sạch, không có NaN — vì NaN sẽ
  làm hỏng `MinMaxScaler` và `window_transform` trong notebook.
- Mất broker → CSV trên Pi vẫn đầy. Mất thẻ nhớ → MQTT vẫn nhận.

Nối vào notebook (mục 6 của Chapter 12): thay cell tải Yahoo Finance bằng

```python
df = pd.read_csv(r"C:\mqtt\data\sensors.csv", parse_dates=["timestamp"])
df = df.set_index("timestamp")
close = df["temperature_c"]        # hoặc df["lux"]
```

# Phụ lục C — Bẫy `lgpio` (Python 3.13)

`adafruit-blinka` kéo về `lgpio`, một C-extension. Wheel prebuilt trên PyPI chỉ có cho
cp39–cp312, trong khi Raspberry Pi OS Trixie dùng Python 3.13 → pip phải compile từ
source.

| Lỗi | Nguyên nhân |
|---|---|
| `command 'swig' failed: No such file or directory` | thiếu `swig` |
| `cannot find -llgpio` | thiếu thư viện C `liblgpio` |
| `pip install lgpio` báo thành công nhưng `import lgpio` lỗi | pip lấy nhầm wheel rỗng `lgpio-0.0.0.2` từ piwheels |

**Cách 1 — wheel prebuilt của Adafruit (khuyến nghị, không cần compile):**

```bash
python3 --version     # 3.13 hay 3.14 để chọn đúng wheel
source venv/bin/activate
pip install https://github.com/adafruit/lgpio-python-wheels/raw/main/wheels/lgpio-0.2.2.0-cp313-cp313-linux_aarch64.whl
# Python 3.14 → đổi cp313 thành cp314
pip install -r requirements-pi.txt
```

**Cách 2 — build từ source:**

```bash
sudo apt install -y swig build-essential python3-dev liblgpio-dev
source venv/bin/activate
pip install -r requirements-pi.txt
```

**Cách 3 — dùng bản có sẵn của RPi OS:**

```bash
sudo apt install -y python3-lgpio
rm -rf venv
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install -r requirements-pi.txt
```

Kiểm tra sau khi cài:

```bash
pip show lgpio | head -2
python -c "import lgpio; print(lgpio.__version__, lgpio.__file__)"
# kỳ vọng 0.2.2.0 — nếu ra 0.0.0.2 thì cài lại bằng Cách 1
```

# Phụ lục D — Broker không đọc được file `passwd`

Nếu broker thoát ngay và log có:

```
password-file: Error: Unable to open pwfile "C:\Program Files\Mosquitto\passwd"
mosquitto version 2.1.2 terminating
```

thì đó là **lỗi quyền file**, không phải sai mật khẩu. File `passwd` tạo trong
`C:\Program Files` bởi một tiến trình khác (hoặc bằng quyền Administrator) thường có ACL
mà tiến trình thường không đọc được.

Cách xử lý: giữ `passwd` + `mosquitto.conf` trong thư mục người dùng như mục 1B.3–1B.4
(`C:\Users\<TEN_BAN>\mosq\`). Nếu đã có file bị chặn quyền thì:

```powershell
icacls "C:\Users\<TEN_BAN>\mosq\passwd" /reset
```

# Phụ lục E — Xử lý sự cố

| Triệu chứng | Nguyên nhân | Cách sửa |
|---|---|---|
| `Checksum did not validate` | thiếu điện trở 10 kΩ pull-up, dây quá dài | gắn điện trở DATA–3.3V, rút ngắn dây |
| DHT11 lỗi xuyên suốt | thiếu `libgpiod2`/`sysv-ipc` → đang bit-bang | `sudo apt install -y libgpiod2 sysv-ipc` rồi reboot |
| `CẢNH BÁO: không có pulseio` | chưa cài 2 gói trên | cài rồi reboot |
| `i2cdetect -y 1` không thấy `23` | sai dây, hoặc địa chỉ `0x5C` | kiểm tra dây; đổi `BH1750_ADDRESS = 0x5C` |
| `Lỗi ghi CSV: [Errno 13] Permission denied` | `csv_path` trỏ vào thư mục không ghi được | đổi `csv_path` trong `config.yaml` sang thư mục của user đang chạy (vd `/home/admin/iot_data/sensors.csv`) |
| Dashboard "Đang chờ ảnh" | Pi chưa chạy client, hoặc sai IP broker | kiểm tra `mqtt.host` trong `config.yaml` = IP laptop, chạy `ping` / `create_connection` từ Pi |
| Metric **Broker** = `Mất kết nối` | broker chưa chạy / sai tài khoản | bật broker (2.3); kiểm tra `pi01` / `MQTT_PASSWORD` trong `.env` |
| Metric **Pi** = `chưa rõ` | chưa có retained status | client chưa kết nối broker lần nào |
| `Connection refused` từ Pi | broker chưa listen 1883 | bật broker; kiểm tra `Get-NetTCPConnection -LocalPort 1883` |
| `timed out` từ Pi | firewall Windows chặn, hoặc khác mạng | cho phép `mosquitto.exe` qua Windows Firewall |
| Ảnh không lên | sai `CAM_INDEX` | thử 1, 2... trong client |
| Hai client camera chạy cùng lúc, ảnh đứng | tranh `/dev/video0` | chỉ chạy một trong `pi_camera_client.py` / `pi_detection_client.py` |
| `KHÔNG TÌM THẤY model` | thiếu `model.pt` | `scp model.pt admin@<IP_PI>:~/BTL_IOT/` |
| `ImportError: libopenblas` / `libc10` | thiếu thư viện hệ thống | `sudo apt install -y libopenblas-dev libopenblas0` |
| Pi OOM khi chạy detection | RAM 4 GB không đủ | giảm `IMGSZ` xuống 256, hoặc export ONNX + `onnxruntime` |
| `ModuleNotFoundError` | chưa kích hoạt venv | `source venv/bin/activate` |

# Phụ lục F — Cấu trúc thư mục

| File | Nơi chạy | Vai trò |
|---|---|---|
| `aiot/collector.py` | Pi (systemd `aiot-collector`) | đọc DHT11, BH1750, PMS7003, MQ135 → SQLite + CSV + MQTT mỗi 10 s (có code, chưa triển khai) |
| `aiot/sensors.py`, `aiot/payload.py` | Pi / cả hai | đọc phần cứng; định dạng CSV và payload MQTT dùng chung |
| `deploy/aiot-collector.service` | Pi | unit systemd của collector |
| `pi_sensor_client.py` | Pi | bản cũ: chỉ DHT11 + BH1750; không chạy cùng collector |
| `pi_detection_client.py` | Pi | chụp ảnh → YOLOv8n → vẽ khung → publish ảnh + JSON mỗi 2 s |
| `pi_camera_client.py` | Pi | chỉ chụp + publish ảnh (bản đơn giản, không YOLO) |
| `server_app.py` | Laptop | nhận ảnh + cảm biến, lưu CSV, dashboard Streamlit |
| `requirements-pi.txt` | Pi | thư viện trong `~/BTL_IOT/venv` |
| `requirements.txt` | Laptop | thư viện cho server + notebook |
| `model.pt` | cả hai | checkpoint YOLOv8n (ultralytics 8.0.166, task `detect`, 1 class `person`) |
| `time_series_data_preprocessing.ipynb` | Laptop | notebook Chapter 12, đọc CSV cảm biến |

```
~/BTL_IOT/
├── venv/                 ← tự tạo, không push lên GitHub
├── model.pt
├── pi_camera_client.py
├── pi_detection_client.py
├── pi_sensor_client.py
├── requirements-pi.txt
├── requirements.txt
├── server_app.py
└── README.md
```
