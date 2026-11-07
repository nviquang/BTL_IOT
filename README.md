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

# Việc cần làm trên Pi

Trạng thái ngày 07/11/2026: code đã chép lên `~/BTL_IOT`, collector đã chạy thử 75 s ở
foreground (đọc đủ cảm biến, ghi CSV + SQLite, dừng sạch). **Chưa cài dịch vụ systemd nào**
(cần mật khẩu sudo). Làm lần lượt, xong bước trước mới sang bước sau:

- [ ] **Bí mật:** đổi mật khẩu MQTT của `pi01` trên laptop (1B.3), rồi tạo `.env` từ
      `.env.example` trên cả laptop và Pi (`MQTT_PASSWORD`). Không commit `.env`.
- [ ] **Collector:** cài và bật `aiot-collector` (2.4a, bước 6); kiểm tra `systemctl status`
      và có dòng mới trong `readings`.
- [ ] **Controller:** chạy demo bằng DB thử khi có người đứng cạnh mạch (2.4c, bước 1), rồi
      mới bật `aiot-controller` (2.4c, bước 2).
- [ ] **Telegram:** tạo bot bằng BotFather, thêm `TELEGRAM_TOKEN` và `ALLOWED_CHAT_IDS` vào
      `.env` trên Pi, bật `aiot-bot` (2.4d).
- [ ] **Gemini và dự báo:** thêm `GEMINI_API_KEY` vào `.env`, bật `aiot-morning.timer` (2.4f)
      và `aiot-forecast.timer` (2.4e). Chép mô hình LSTM lên Pi nếu muốn dùng (2.4g).
- [ ] **Nắng/mưa và ảnh:** bật `aiot-weather.timer`, khởi động lại `aiot-bot`, thử `/photo` (2.4h).
- [ ] **Nghiệm thu:** thu dữ liệu ít nhất 7 ngày, rồi chạy
      `venv/bin/python scripts/acceptance_report.py --start <ngày đầu> --end <ngày cuối>` và
      ghi kết quả vào `docs/nghiem_thu.md`.

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

> **Trạng thái: có code, đã chạy thử 75 s ở foreground trên Pi ngày 07/11/2026 (bước 5),
> chưa cài dịch vụ systemd (bước 6).**

`aiot/collector.py` đọc **cả 4 cảm biến** (DHT11, BH1750, PMS7003, MQ135 DO) mỗi 10 giây:

- ghi mẫu thô vào SQLite `raw_readings` (`db_path` trong `config.yaml`), kể cả khi DHT11
  lỗi; gộp trung bình 5 phút vào `readings` mỗi phút (chỉ khi đồng hồ tin được, xem dưới);
  xoá mẫu thô cũ hơn 7 ngày mỗi ngày một lần;
- ghi `sensors.csv` **đúng định dạng cũ** (chỉ khi có đủ DHT11 và lux, và đồng hồ tin được;
  MQTT cũng vậy, để CSV/dashboard không nhận dòng sai giờ);
- gửi MQTT `nha/pi01/sensor` (payload cũ thêm `pm1`, `pm25`, `pm10`, `mq135_do`) nếu
  `mqtt.enabled` và có `MQTT_PASSWORD`; mất broker thì vẫn thu thập;
- `mq135_do` để trống trong 180 giây đầu vì MQ135 cần sấy.

**Đồng hồ "tin được"** (`aiot.collector.is_clock_trusted`, dùng chung cho collector, controller
qua bot, forecast và báo cáo sáng): đã đồng bộ NTP **ít nhất một lần từ lúc khởi động**, tức là
có file `/run/systemd/timesync/synchronized` (systemd-timesyncd tạo ở lần đồng bộ đầu, mất khi
khởi động lại). Không dùng `NTPSynchronized=yes` vì giá trị này về `no` khoảng 9 giờ sau khi mất
Internet dù đồng hồ vẫn đúng, làm dừng gộp dữ liệu, dự báo và lệnh điều khiển. Chỉ khi không có
file đó (ví dụ máy dùng chrony thay timesyncd) mới hỏi `timedatectl`. Trong lúc chưa tin được,
mẫu vẫn vào `raw_readings` với `clock_synced=0` nhưng không ghi CSV/MQTT. **Chưa kiểm chứng trên
Pi sau một lần mất mạng dài** (cần thử: rút Wi-Fi > 9 giờ, kiểm tra `readings` vẫn được gộp).
Sau mất điện, đồng hồ có thể khôi phục về giờ cũ hơn mẫu đã lưu: collector ghi cảnh báo
"ts=... <= MAX(raw_readings.ts)" khi mẫu mới trùng ts bị bỏ.

**Không bao giờ chạy collector và `pi_sensor_client.py` cùng lúc**: hai tiến trình
tranh nhau DHT11, BH1750 và cổng UART. `pi_sensor_client.py` được giữ lại làm bản cũ.

Triển khai (một lần):

```bash
# 0) Đưa code lên Pi. Nhánh feat/aiot-core chưa push lên GitHub, nên chép file
#    từ laptop (Git Bash, ở thư mục repo) bằng scp:
#      scp -r aiot deploy scripts config.yaml requirements-pi.txt admin@<IP_PI>:~/BTL_IOT/
#    Khi nhánh đã push thì dùng: cd ~/BTL_IOT && git pull
#    .env (MQTT_PASSWORD) chép riêng và không commit; thiếu .env thì collector bỏ qua MQTT.

# 1) Múi giờ (CSV ghi giờ địa phương) và kiểm tra đồng bộ NTP
sudo timedatectl set-timezone Asia/Ho_Chi_Minh
ls /run/systemd/timesync/synchronized          # có file: đã đồng bộ NTP ít nhất một lần từ lúc khởi động
timedatectl show -p NTPSynchronized --value     # dự phòng khi không có file trên

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

### 2.4c. Bộ điều khiển quạt (`aiot/controller.py`)

> **Trạng thái: có code và unit test, CHƯA KIỂM TRA TRÊN MẠCH.** Hai bước dưới (demo có
> người đứng cạnh, rồi bật dịch vụ thật) do người vận hành tự làm.

Controller đọc PM2.5 thô trong SQLite mỗi `loop_s` giây (20 s), chạy luật trong
`aiot/control.py` và bật/tắt quạt qua relay GPIO23 (HIGH = quay). Chỉ controller được giữ
GPIO23: không chạy cùng `test_all.py`, và không chạy bản demo cùng dịch vụ thật.
Khởi động lại thì khôi phục trạng thái cuối trong DB (đang BẢO VỆ thì quạt quay lại ngay);
dừng dịch vụ (SIGTERM/Ctrl+C) thì quạt tắt. Mọi chuyển trạng thái, ghi đè, lỗi relay
nằm trong bảng `events`.

**Bước 1 — Demo trên mạch với DB thử** (có người đứng cạnh quạt; code đã chép lên Pi như 2.4a):

```bash
cd ~/BTL_IOT && source venv/bin/activate
sudo systemctl stop aiot-controller 2>/dev/null   # nếu đã cài dịch vụ thật

# Config demo: DB thử + demo: true (mọi thời gian chờ còn 20 s). Không dùng DB thật.
sed -e 's#^db_path:.*#db_path: /home/admin/iot_data/demo.db#' -e 's#^  demo: false#  demo: true#' config.yaml > /tmp/demo.yaml
grep -E '^db_path|^  demo:' /tmp/demo.yaml       # phải thấy demo.db và demo: true

# Terminal 1: controller dùng config demo (Ctrl+C để dừng, quạt sẽ tắt)
python -m aiot.controller --config /tmp/demo.yaml

# Terminal 2: bơm PM2.5 giả mỗi 10 s
python scripts/inject_pm.py --db ~/iot_data/demo.db --profile high     # 80: quạt quay trong ~1 phút
python scripts/inject_pm.py --db ~/iot_data/demo.db --profile low      # 20: quạt dừng sau ~1 phút
python scripts/inject_pm.py --db ~/iot_data/demo.db --profile danger   # 200: quạt quay ngay (~30 s)
python scripts/inject_pm.py --db ~/iot_data/demo.db --profile step --phase 12   # 2 phút 20, 2 phút 80, 2 phút 20

# Kiểm tra trong lúc quạt chạy
vcgencmd get_throttled                           # phải là throttled=0x0
sqlite3 ~/iot_data/demo.db "select datetime(ts,'unixepoch','localtime'), kind, detail from events order by rowid desc limit 10;"
```

Thử khởi động lại khi đang BẢO VỆ: để quạt quay (profile `high`), rút nguồn Pi, cắm lại,
chạy lại Terminal 1 và 2 → quạt quay lại ngay sau khi controller khởi động.
Tắt Pi và rút nguồn trước khi đổi dây.

**Bước 2 — Bật dịch vụ thật** (sau khi demo đạt; `demo: false` trong `config.yaml`):

```bash
cd ~/BTL_IOT
grep -E '^  demo:' config.yaml                   # phải là demo: false
sudo cp deploy/aiot-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiot-controller
journalctl -u aiot-controller -f                 # thấy "Controller bắt đầu: ... mode=NORMAL"
```

Dừng: `sudo systemctl stop aiot-controller` (quạt tắt). Ghi đè thủ công sẽ đến từ bot
Telegram (khoá state `pending_override`); trước khi có bot, có thể thử bằng tay:
`sqlite3 ~/iot_data/aiot.db "insert or replace into state values('pending_override', json_object('fan', json('true'), 'ttl_s', 600, 'at', unixepoch()));"`
(lệnh thiếu `at` hoặc lệch quá `telegram.stale_command_s + control.loop_s` giây bị bỏ với sự kiện
`override_stale`, để lệnh cũ không chạy khi controller khởi động lại sau nhiều giờ).

Sau khi Pi khởi động lại, controller chỉ dùng mẫu PM ghi từ lúc khởi động (bỏ mẫu trước khi tắt
máy), xoá mốc "điều kiện đúng liên tục" đã lưu, và trong `stale_s` giây đầu không báo lỗi cảm
biến khi collector chưa kịp ghi mẫu đầu. Relay lỗi thì có cảnh báo `relay_error` qua Telegram.
Mỗi vòng controller ghi khoá `controller_heartbeat`; `/status`, `/fan`, `/mode` cảnh báo khi
khoá này cũ hơn 3 vòng `loop_s` (dịch vụ không chạy).

### 2.4d. Bot Telegram (`aiot/bot.py`)

> **Trạng thái: có code và unit test, CHƯA THỬ VỚI BOT THẬT.** Các bước dưới do nhóm tự làm;
> không ghi token vào tài liệu, chat nhóm hay commit.

Bot dùng long polling (không cần mở port), chỉ trả lời chat trong `ALLOWED_CHAT_IDS`, và
gửi mọi tin trong bảng `outbox` (cảnh báo của controller, gửi bù khi có mạng lại). Bot không
chạm GPIO: `/fan`, `/mode` chỉ ghi khoá `pending_override` để controller áp dụng trong
vòng `loop_s` giây. Lệnh: `/now`, `/status`, `/fan on|off`, `/mode auto|manual`,
`/quiet 22 6`, `/forecast` (6 giờ tới, nhãn AQI, tên mô hình; xem 2.4e), `/photo` (xem 2.4h), `/help`, `/report`
(báo cáo 24 giờ qua, trả lời ngay, có thể gọi Gemini; xem 2.4f). Lệnh điều khiển lệch quá 120 s so với đồng hồ Pi (ví dụ gửi lúc mất mạng)
bị bỏ; khi đồng hồ Pi chưa tin được (chưa đồng bộ NTP lần nào từ lúc khởi động; Pi không có
RTC) thì lệnh điều khiển bị từ chối, lệnh chỉ đọc vẫn trả lời. `/report` cũ hơn 120 s (tồn đọng
khi bot chạy lần đầu hoặc sau khi mất mạng) cũng bị bỏ, để không gọi Gemini cho từng tin cũ.

**Bước 1 — Tạo bot:** trong Telegram nhắn `@BotFather` → `/newbot` → đặt tên → nhận token
dạng `123456789:AA...`. Giữ token như mật khẩu; lộ thì `/revoke` trong BotFather để đổi.

**Bước 2 — Tìm chat id:** mỗi thành viên nhắn một tin bất kỳ cho bot (hoặc thêm bot vào
nhóm rồi nhắn trong nhóm), sau đó mở trên máy của mình (không dán link này vào đâu cả):
`https://api.telegram.org/bot<TOKEN>/getUpdates` và đọc `message.chat.id` (nhóm có id âm).
Nếu thấy lỗi 409 "webhook" thì gọi `.../bot<TOKEN>/deleteWebhook` một lần.

**Bước 3 — Điền `.env` trên Pi** (`~/BTL_IOT/.env`, đã nằm trong `.gitignore`):

```bash
cd ~/BTL_IOT
nano .env          # thêm 2 dòng, nhiều chat cách nhau bằng dấu phẩy
#   TELEGRAM_TOKEN=<token từ BotFather>
#   ALLOWED_CHAT_IDS=<id1>,<id2>
chmod 600 .env
```

ID sai định dạng bị bỏ kèm cảnh báo trong log (các dịch vụ khác vẫn chạy). Thiếu token hoặc
không còn id hợp lệ nào thì bot ghi lỗi và thoát mã 2, systemd không tự khởi động lại.

**Bước 4 — Cài dịch vụ:**

```bash
cd ~/BTL_IOT && source venv/bin/activate && pip install -r requirements-pi.txt   # có requests
sudo cp deploy/aiot-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiot-bot
journalctl -u aiot-bot -f                        # thấy "Bot bắt đầu: ... chat được phép"
```

**Bước 5 — Kiểm tra thật** (controller và collector đang chạy):

1. Gửi lần lượt `/help`, `/now`, `/status`, `/quiet 22 6`, `/mode manual`, `/mode auto`: mỗi lệnh có trả lời.
2. `/fan on`: quạt quay trong khoảng `loop_s` (20 s); `/status` thấy ghi đè và giờ hết hạn;
   `/mode auto` thì quạt về theo luật.
3. Từ một tài khoản không có trong `ALLOWED_CHAT_IDS`: gửi `/now` → không có trả lời.
4. Tắt Wi-Fi 10 phút (`sudo nmcli radio wifi off`, có người đứng cạnh, bật lại bằng
   `sudo nmcli radio wifi on` hoặc qua bàn phím/màn hình): quạt và thu dữ liệu vẫn chạy;
   có mạng lại thì tin trong outbox được gửi bù (cảnh báo cũ hơn 1 giờ được gộp thành một tin).
5. Token không lọt vào log: `journalctl -u aiot-bot | grep -c "<phần đầu token, ví dụ 9 chữ số trước dấu :>"` phải ra `0`.
6. Nhật ký lệnh: `sqlite3 ~/iot_data/aiot.db "select datetime(ts,'unixepoch','localtime'), detail from events where kind='command' order by rowid desc limit 10;"`

### 2.4e. Dự báo PM2.5 mỗi giờ (`aiot/forecast.py`)

Mỗi giờ (phút 02) timer chạy một lần dự báo PM2.5 trung bình giờ cho 6 giờ tới từ bảng
`readings` và ghi vào bảng `forecasts`. Nếu có `models/pm25_lstm.tflite` + `models/scaler.json`
(đường dẫn trong `config.yaml` mục `forecast`) và thư viện `ai-edge-litert` thì dùng LSTM
(`model = 'lstm'`); thiếu file, file hỏng, thiếu thư viện hay LSTM lỗi thì dùng mô hình đối
chứng: `seasonal_naive` (cùng giờ hôm qua) khi có đủ 24 giờ dữ liệu, ngược lại `persistence`
(giữ nguyên giờ gần nhất). Hiện **chưa chép LSTM lên Pi** (xem 2.4g). Không có giờ hợp lệ nào trong 3 giờ gần đây thì không ghi dự báo
(vẫn thoát 0). Đồng hồ Pi chưa đồng bộ NTP (vừa khởi động, chưa có mạng) thì bỏ lần chạy đó
(log "chưa đồng bộ NTP", thoát 0), để lần chạy bù lúc khởi động không ghi dự báo với giờ sai. Controller dùng TB 3 giờ đầu cho luật "Chủ động" (> 100 µg/m³ thì bật quạt
trước); dự báo cũ hơn `control.forecast_max_age_s` (5400 s) bị bỏ qua. Bot trả lời `/forecast`.

```bash
cd ~/BTL_IOT && source venv/bin/activate
python -c "import numpy; print(numpy.__version__)"   # cần numpy (đã có trong requirements-pi.txt)
python -m aiot.forecast                              # chạy thử một lần, xem log "Dự báo made_at=..."
sudo cp deploy/aiot-forecast.service deploy/aiot-forecast.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiot-forecast.timer
systemctl list-timers aiot-forecast.timer           # thấy lần chạy kế tiếp ở phút 02
journalctl -u aiot-forecast -n 20
# Sau một giờ phải có dòng mới:
sqlite3 ~/iot_data/aiot.db "select datetime(made_at,'unixepoch','localtime'), horizon_h, round(pm25_pred,1), model from forecasts order by made_at desc, horizon_h limit 6;"
```

### 2.4f. Báo cáo sáng và lời khuyên Gemini (`aiot/report.py`, `aiot/advice.py`)

> **Trạng thái: có code và unit test (client Gemini giả), CHƯA THỬ VỚI API KEY THẬT.**
> Không ghi API key vào tài liệu, chat nhóm hay commit.

Mỗi ngày 06:30 (`OnCalendar` trong `deploy/aiot-morning.timer`) timer lập báo cáo 24 giờ qua (PM2.5 TB/thấp/cao kèm nhãn AQI, số giờ theo
nhóm, nhiệt độ, độ ẩm, độ phủ dữ liệu, dự báo 6 giờ kèm tên mô hình, trạng thái quạt, MQ135
nếu có vượt ngưỡng) và ghi vào outbox; bot gửi (gửi bù khi có mạng lại). Mỗi ngày chỉ một
bản (khoá `morning_report_date`); `Persistent=true` chạy bù nếu Pi tắt lúc 06:30. Đồng hồ
chưa đồng bộ NTP thì chờ tối đa 15 phút rồi vẫn gửi, kèm ghi chú giờ có thể sai.

Gemini chỉ viết lời khuyên (`{headline, advice}`, temperature 0.2), chỉ nhận số liệu tổng hợp
(không có device id, chat id, mẫu thô), không điều khiển quạt. Tối đa
`gemini.max_calls_per_day` (3) lần/ngày, `/report` cũng tính. Không có model/API key/thư viện,
lỗi mạng, hết hạn mức, quá `timeout_s`, JSON sai hoặc quá 800 ký tự: dùng lời khuyên mẫu theo
nhóm AQI (ghi "(lời khuyên mẫu)"), bản tin vẫn gửi. Model đặt ở `gemini.model` trong
`config.yaml`; kiểm tra lại tên model và hạn mức hiện hành trước khi triển khai.

```bash
cd ~/BTL_IOT && source venv/bin/activate && pip install -r requirements-pi.txt   # có google-genai
nano .env          # thêm: GEMINI_API_KEY=<key từ Google AI Studio>; giữ chmod 600 .env
python -m aiot.report                                # chạy thử: thêm 1 tin morning_report vào outbox
sudo cp deploy/aiot-morning.service deploy/aiot-morning.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiot-morning.timer
systemctl list-timers aiot-morning.timer            # lần chạy kế tiếp lúc 06:30
journalctl -u aiot-morning -n 20
sqlite3 ~/iot_data/aiot.db "select key, value from state where key like 'gemini_calls:%' or key = 'morning_report_date';"
```

**Đổi giờ báo cáo:** `report.time` trong `config.yaml` **không** điều khiển timer; giờ chạy
thật nằm ở `OnCalendar=*-*-* 06:30:00` trong `deploy/aiot-morning.timer`. Muốn đổi giờ phải sửa
cả hai (timer rồi `sudo systemctl daemon-reload`), vì `scripts/acceptance_report.py` dùng
`report.time` để chấm báo cáo đúng giờ.

Lưu ý: `python -m aiot.report` chạy thử trong ngày sẽ ghi `morning_report_date`, nên timer
06:30 cùng ngày không gửi lại; muốn thử lại thì xoá khoá đó bằng sqlite3.

**Kiểm tra thật (nhóm tự làm, cần API key thật):**

1. Có `GEMINI_API_KEY` đúng và bot đang chạy: gửi `/report` → lời khuyên không có chữ
   "(lời khuyên mẫu)"; `journalctl -u aiot-bot` không có cảnh báo "Gọi Gemini lỗi".
2. Đặt key sai trong `.env` (`sudo systemctl restart aiot-bot`), gửi `/report` → vẫn có báo
   cáo, có "(lời khuyên mẫu)", log có "Gọi Gemini lỗi" và không in key. Đặt lại key đúng.
3. Gửi `/report` quá 3 lần trong ngày → các lần sau dùng lời khuyên mẫu.

### 2.4g. Huấn luyện LSTM và đưa lên Pi (`training/train_lstm.py`, `training/02_danh_gia.ipynb`)

Huấn luyện trên laptop (TensorFlow CPU, chỉ cài trong `.venv`, không cài lên Pi):

```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt -r training/requirements.txt
.venv/Scripts/python -m training.train_lstm      # ~vài phút, ghi models/ (không commit)
.venv/Scripts/jupyter nbconvert --to notebook --execute --inplace training/02_danh_gia.ipynb
```

`models/` gồm `pm25_lstm.tflite` (batch 1, chỉ op có sẵn của TFLite), `scaler.json` (đặc
trưng, min/max MinMaxScaler fit trên train, dạng đích, n_in=24, n_out=6), `pm25_lstm.keras`
và `train_summary.json`. Kết quả đánh giá và kết luận nằm trong notebook. Tóm tắt (07/11/2026,
dữ liệu công khai CAMS): LSTM vượt persistence ở mọi horizon 1–6 giờ (MAE test giảm 21,7–31,1%),
nhưng chỉ nhỉnh hơn hồi quy Ridge trên đặc trưng trễ rất ít.

**Quy tắc:** mô hình chỉ được chép lên Pi khi **vượt persistence ở horizon ≥ 2 giờ trên dữ
liệu của Pi** (không chỉ trên dữ liệu công khai CAMS). Hiện Pi mới có vài ngày dữ liệu, nên
**chưa triển khai**; sản phẩm dùng mô hình đối chứng. Khi đạt, các bước là:

```bash
# Trên Pi: cài LiteRT vào venv (đã thử: ai-edge-litert 2.3.0 cài được trên Python 3.13)
ssh admin@<IP_PI> "cd ~/BTL_IOT && mkdir -p models && venv/bin/pip install ai-edge-litert && venv/bin/python -c 'from ai_edge_litert.interpreter import Interpreter; print(\"ok\")'"
# Trên laptop (Git Bash, thư mục repo): chép mô hình
scp models/pm25_lstm.tflite models/scaler.json admin@<IP_PI>:~/BTL_IOT/models/
# Trên Pi: chạy một lần, log phải có "Đã nạp mô hình học máy" và "suy luận trong ... ms"
cd ~/BTL_IOT && venv/bin/python -m aiot.forecast
sqlite3 ~/iot_data/aiot.db "select model, count(*) from forecasts group by model;"
# kỳ vọng có dòng model = 'lstm'. Xoá file mô hình thì lần chạy sau về seasonal_naive/persistence.
```

### 2.4h. Cảnh báo nắng/mưa và ảnh (`aiot/weather.py`, `aiot/camera.py`)

> **Trạng thái: có code và unit test, CHƯA THỬ THẬT** (chưa thử với nắng/mưa thật, camera
> thật và bot thật).

DHT11, BH1750 và camera USB (`/dev/video0`) đặt ngoài ban công. Mỗi 5 phút
`aiot-weather.timer` chạy `python -m aiot.weather` một lần, xét bảng `readings` (trung bình
5 phút, không cần `valid=1`):

| Cảnh báo | Điều kiện (mục `weather` trong `config.yaml`) |
| -------- | --------------------------------------------- |
| Nắng gắt | lux > 30000 **và** nhiệt độ ≥ 33 °C |
| Có thể sắp mưa | độ ẩm ≥ 85% **và** lux < 5000, chỉ từ 8h tới 16h |

Điều kiện phải đúng ở 3 cửa sổ 5 phút liền nhau, cửa sổ mới nhất không cũ quá 10 phút
(thiếu dữ liệu thì không kết luận). Chỉ báo khi **chuyển sang** trạng thái đó, tối đa 1 tin
mỗi loại mỗi 3 giờ, theo giờ yên lặng chung (`/quiet`). Tin bị chặn (chưa hết 3 giờ hoặc
đang giờ yên lặng) được gửi ở lần chạy đầu tiên được phép nếu điều kiện vẫn đúng. Mất dữ
liệu lâu hơn 3 giờ thì trạng thái đã nhớ bị bỏ, điều kiện quay lại sẽ được báo lại. Khi
báo, job chụp một ảnh trong tiến trình con có timeout 20 s (lưu ở
`/home/admin/iot_data/photos`, xoá sau 7 ngày) và ghi tin kèm ảnh vào `outbox`; chụp lỗi thì
gửi chữ kèm "(không chụp được ảnh)". Tin chờ quá 1 giờ (mất mạng) bị gộp vào tin tóm tắt,
không gửi ảnh. Đây là **ước lượng từ cảm biến, không phải dự báo thời tiết**; đồng hồ Pi
chưa đồng bộ NTP thì job bỏ qua lần chạy đó.

Lệnh `/photo` của bot chụp và gửi ảnh ngay (lệnh cũ hơn 120 s bị bỏ, giống `/report`).
Camera không trả ảnh trong 20 s thì bot trả lời "Camera không phản hồi, thử lại sau." và
vẫn nhận lệnh khác. Khi chạy, bot đặt menu lệnh của Telegram (gõ `/` để thấy danh sách);
lỗi thì thử lại sau lần nhận tin thành công kế tiếp.

```bash
cd ~/BTL_IOT && git pull
timedatectl | grep "Time zone"             # phải là Asia/Ho_Chi_Minh (giờ ngày, giờ yên lặng)
venv/bin/python -m aiot.camera /tmp/thu.jpg && echo "chụp được"   # thử camera
venv/bin/python -m aiot.weather            # chạy thử một lần, xem log
sudo cp deploy/aiot-weather.* /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now aiot-weather.timer
sudo systemctl restart aiot-bot            # nạp code mới: gửi ảnh, /photo, menu lệnh
systemctl list-timers aiot-weather.timer
```

Lưu ý:
- **Múi giờ:** `timedatectl` phải hiện `Asia/Ho_Chi_Minh`; sai múi giờ thì khung 8h–16h và
  giờ yên lặng lệch (`sudo timedatectl set-timezone Asia/Ho_Chi_Minh`).
- **Báo nhầm lúc bình minh/hoàng hôn:** trời mờ + ẩm cao giống "sắp mưa". Khung mặc định đã
  thu về 8h–16h (Hà Nội tháng 11: mặt trời mọc ~06:10, lặn ~17:15); **kiểm tra trong đợt
  chạy 7 ngày** xem còn báo nhầm không, rồi chỉnh `day_start_h`/`day_end_h`/`rain_lux`.
- **Camera dùng chung:** mỗi lần chụp mở rồi đóng `/dev/video0`. Nếu `pi_camera_client.py`
  (hoặc `pi_detection_client.py` qua nó) đang chạy thì camera bận: cảnh báo được gửi chữ,
  `/photo` trả lời "Không chụp được ảnh". Muốn có ảnh thì không chạy client camera cũ cùng lúc.
- **DHT11 phải được che nắng trực tiếp** (hộp thông gió, không kín): phơi nắng sẽ đọc nhiệt
  độ cao hơn không khí nhiều và báo nắng gắt sai.
- **Mạch không chống nước:** Pi, breadboard, cảm biến và camera phải có mái che mưa hắt.
- **BH1750 bão hoà khoảng 65000 lux:** nắng trưa trực tiếp có thể đọc kịch trần, vẫn đủ để
  vượt ngưỡng 30000.

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

Mục này kiểm tra nhanh luồng MQTT + dashboard. Nghiệm thu 7 ngày của trạm AIoT (ổn định, điều
khiển, Telegram, Gemini, an toàn) xem `docs/nghiem_thu.md` và `scripts/acceptance_report.py`.

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
