# Vận hành chi tiết — trạm quan trắc không khí AIoT

Các bước thủ công đầy đủ. Cách nhanh (một lệnh) nằm ở [README](../README.md#chạy-nhanh):
`scripts\up.ps1` trên laptop hoặc `scripts/pi_up.sh` trên Pi tự làm các mục 1, 3.1, 5, 6.4 và 7.
Đọc tài liệu này khi cần hiểu từng bước, chạy demo quạt (5.3), huấn luyện LSTM (8) hoặc gỡ lỗi (10).

Tài liệu liên quan: cách nối dây ở [so_do_noi_day_raspberry_pi.md](../so_do_noi_day_raspberry_pi.md),
quy tắc dự án ở [CLAUDE.md](../CLAUDE.md), bảng nghiệm thu ở [nghiem_thu.md](nghiem_thu.md).
---

## Mục lục

0. [Tổng quan](#0-tổng-quan)
1. [Chuẩn bị Raspberry Pi (một lần)](#1-chuẩn-bị-raspberry-pi-một-lần)
2. [Chuẩn bị laptop (một lần)](#2-chuẩn-bị-laptop-một-lần)
3. [Bí mật và cấu hình](#3-bí-mật-và-cấu-hình)
4. [Kiểm tra phần cứng](#4-kiểm-tra-phần-cứng)
5. [Cài và bật các dịch vụ trên Pi](#5-cài-và-bật-các-dịch-vụ-trên-pi)
6. [Dùng hằng ngày](#6-dùng-hằng-ngày)
7. [Dashboard và camera trên laptop (tuỳ chọn)](#7-dashboard-và-camera-trên-laptop-tuỳ-chọn)
8. [Phần AI: phân tích, huấn luyện, đưa LSTM lên Pi](#8-phần-ai-phân-tích-huấn-luyện-đưa-lstm-lên-pi)
9. [Nghiệm thu 7 ngày](#9-nghiệm-thu-7-ngày)
10. [Xử lý sự cố](#10-xử-lý-sự-cố)
- [Phụ lục](#phụ-lục)

---

## 0. Tổng quan

### Luồng hoạt động

```
                         RASPBERRY PI (chạy liên tục, không cần Internet)
 DHT11, BH1750,  ──► aiot-collector (10 s) ──► SQLite ~/iot_data/aiot.db ◄── aiot-forecast (mỗi giờ :02)
 PMS7003, MQ135        │                          │      ▲                  dự báo PM2.5 1–6 giờ
                       ├─► sensors.csv            │      │
                       └─► MQTT ──────────────────┼──────┼─────────► Laptop: Mosquitto + server_app.py
                                                  ▼      │                 (dashboard, tuỳ chọn)
                       aiot-controller (20 s) ────┘      │
                       luật tất định ──► GPIO23 ──► relay ──► quạt
                                                         │
                       aiot-bot (Telegram) ◄─────────────┘ gửi tin chờ trong outbox, nhận lệnh
                       aiot-morning (06:30) ──► Gemini (số liệu tổng hợp) ──► báo cáo sáng
                       aiot-weather (5 phút) ──► camera USB ──► cảnh báo nắng / sắp mưa kèm ảnh
```

- **Local-first:** đo, lưu, dự báo và điều khiển chạy trên Pi. Mất mạng vẫn đo, vẫn điều khiển;
  tin Telegram được gửi bù khi có mạng.
- **An toàn AI:** Gemini chỉ viết lời khuyên. Bật/tắt quạt là luật cố định trong `aiot/control.py`.
- Các dịch vụ chỉ nói chuyện với nhau qua một file SQLite. Chỉ `aiot-controller` được điều khiển GPIO23.

### Các dịch vụ trên Pi

| Dịch vụ (systemd) | Chạy | Việc |
| ----------------- | ---- | ---- |
| `aiot-collector` | liên tục | đọc cảm biến mỗi 10 s → SQLite + CSV + MQTT, gộp trung bình 5 phút |
| `aiot-controller` | liên tục | luật quạt mỗi 20 s, lệnh `/fan`, `/mode` |
| `aiot-bot` | liên tục | bot Telegram, gửi cảnh báo và báo cáo |
| `aiot-forecast.timer` | mỗi giờ, phút 02 | dự báo PM2.5 6 giờ tới |
| `aiot-morning.timer` | 06:30 hằng ngày | báo cáo sáng + lời khuyên Gemini |
| `aiot-weather.timer` | mỗi 5 phút | luật nắng/mưa, chụp ảnh khi cảnh báo |

### Phần cứng

Raspberry Pi 4 Model B 4 GB. Số chân là số vật lý trên header 40 chân.

| Thiết bị | Dữ liệu | Kết nối |
| -------- | ------- | ------- |
| DHT11 | nhiệt độ, độ ẩm | OUT → pin 11 (GPIO17) |
| BH1750 | ánh sáng (lux) | I2C pin 3, 5; địa chỉ `0x23` |
| PMS7003 | PM1.0, PM2.5, PM10 | UART pin 8, 10 (`/dev/serial0`); 5V |
| MQ135 | 1 bit vượt ngưỡng (`0` = vượt) | DO qua cầu chia áp 10 kΩ/20 kΩ → pin 37 (GPIO26) |
| Relay + quạt 5V | mô phỏng máy lọc | IN → pin 16 (GPIO23); HIGH = quạt quay |
| Camera USB | ảnh | `/dev/video0` |

**Tắt Pi và rút cáp nguồn trước khi đổi dây.** Chi tiết dây, rail breadboard và các lỗi hay
gặp xem `so_do_noi_day_raspberry_pi.md`.

---

## 1. Chuẩn bị Raspberry Pi (một lần)

Pi dùng user `admin`, code ở `~/BTL_IOT`, venv ở `~/BTL_IOT/venv`, dữ liệu ở `~/iot_data`.
Trong tài liệu, `<IP_PI>` là IP của Pi (hiện `10.157.117.41`, hoặc `pi.local`); laptop và Pi
phải cùng mạng Wi-Fi.

### 1.1. Gói hệ thống, I2C, UART, múi giờ

```bash
sudo apt update
sudo apt install -y libgpiod2 sysv-ipc i2c-tools python3-venv sqlite3
sudo raspi-config nonint do_i2c 0            # bật I2C (BH1750)
sudo raspi-config nonint do_serial_hw 0      # bật UART phần cứng (PMS7003)
sudo raspi-config nonint do_serial_cons 1    # tắt serial console trên UART
sudo timedatectl set-timezone Asia/Ho_Chi_Minh
sudo usermod -aG gpio,i2c,dialout,video admin
sudo reboot
```

- `libgpiod2` + `sysv-ipc` bắt buộc cho DHT11; thiếu thì lỗi `Checksum did not validate` liên tục.
- Múi giờ phải là `Asia/Ho_Chi_Minh`: CSV, báo cáo 06:30, giờ yên lặng và khung giờ của cảnh báo
  mưa đều dùng giờ địa phương.

### 1.2. Đưa code lên Pi

Khi nhánh đã có trên GitHub:

```bash
git clone <URL_GITHUB> ~/BTL_IOT          # lần đầu
cd ~/BTL_IOT && git pull                  # các lần sau
```

Khi nhánh chưa push (đang làm trên laptop), chép từ laptop bằng Git Bash ở thư mục repo:

```bash
git archive HEAD aiot deploy scripts config.yaml requirements-pi.txt requirements-pi-camera.txt test_all.py README.md | ssh admin@<IP_PI> "cd ~/BTL_IOT && tar -xf -"
```

`git archive` lấy đúng bản đã commit và giữ kiểu xuống dòng LF (file `.service` có CRLF sẽ
làm systemd đọc sai). File `.env` không nằm trong git, chép riêng ở mục 3.

### 1.3. venv và thư viện

```bash
cd ~/BTL_IOT
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/pip install -r requirements-pi.txt
```

- Nếu cài `lgpio` lỗi (`swig failed`, `cannot find -llgpio`, hoặc cài xong mà `import lgpio`
  vẫn lỗi) xem [Phụ lục C](#phụ-lục-c--bẫy-lgpio-python-313).
- `requirements-pi.txt` chỉ có thư viện lõi (kể cả `opencv-python-headless` cho camera). Nếu venv
  đã có `cv2` từ `opencv-python` thì đừng cài thêm bản headless (hai gói cùng ghi thư mục `cv2`);
  `scripts/pi_up.sh` tự bỏ dòng đó.
- Client nhận diện người (tuỳ chọn) cần `requirements-pi-camera.txt` (`ultralytics`). Cài torch CPU
  **trước** để không kéo bản CUDA ~2 GB:
  `venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision`, rồi
  `venv/bin/pip install -r requirements-pi-camera.txt` (hoặc `pi_up.sh --with-yolo`).

Kiểm tra nhanh:

```bash
venv/bin/python -c "import aiot.collector, aiot.controller, aiot.bot, aiot.forecast, aiot.report, aiot.weather; print('ok')"
```

---

## 2. Chuẩn bị laptop (một lần)

Laptop Windows chỉ cần cho: dashboard MQTT (tuỳ chọn), chạy test, và huấn luyện LSTM.

### 2.1. Python

```powershell
cd C:\Users\nviquang\Documents\University\FourthYear\IoT\BTL\Chapter12
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt     # test
.venv\Scripts\python -m pip install -r requirements.txt         # dashboard (streamlit, paho-mqtt)
.venv\Scripts\python -m pytest                                   # phải toàn bộ PASS
```

### 2.2. Broker MQTT (Mosquitto)

1. Cài Mosquitto từ <https://mosquitto.org/download/> (mặc định vào `C:\Program Files\Mosquitto`).
2. Tạo user `pi01` (lệnh hỏi mật khẩu hai lần; thêm `-c` chỉ khi tạo file lần đầu):
   ```powershell
   mkdir C:\Users\<TEN_BAN>\mosq -Force
   & "C:\Program Files\Mosquitto\mosquitto_passwd.exe" -c "C:\Users\<TEN_BAN>\mosq\passwd" pi01
   ```
   Đổi mật khẩu sau này: chạy lại **không có `-c`** (không cần mật khẩu cũ).
3. Tạo `C:\Users\<TEN_BAN>\mosq\mosquitto.conf`:
   ```conf
   listener 1883
   allow_anonymous false
   password_file C:\Users\<TEN_BAN>\mosq\passwd
   ```
4. Cho phép `mosquitto.exe` qua Windows Firewall (mạng Private).

Đặt `passwd` và `mosquitto.conf` trong thư mục người dùng, không trong `C:\Program Files`
(xem [Phụ lục D](#phụ-lục-d--broker-không-đọc-được-file-passwd)).

---

## 3. Bí mật và cấu hình

### 3.1. File `.env` (không commit)

Chép `.env.example` thành `.env` ở thư mục repo, điền:

| Khoá | Lấy ở đâu |
| ---- | --------- |
| `TELEGRAM_TOKEN` | Telegram: nhắn `@BotFather` → `/newbot` → nhận token dạng `123456789:AA...` |
| `ALLOWED_CHAT_IDS` | mã chat được phép điều khiển bot (cách nhau dấu phẩy), xem dưới |
| `GEMINI_API_KEY` | Google AI Studio → API key |
| `MQTT_PASSWORD` | mật khẩu user `pi01` đặt ở 2.2 |

**Lấy chat id:** gửi `/start` cho bot (muốn cả nhóm dùng thì thêm bot vào nhóm và nhắn một
tin), rồi mở trên máy của bạn `https://api.telegram.org/bot<TOKEN>/getUpdates` và đọc
`message.chat.id` (chat nhóm có id âm). Không dán link có token vào đâu cả.

Chép `.env` lên Pi và khoá quyền đọc:

```bash
scp .env admin@<IP_PI>:~/BTL_IOT/.env
ssh admin@<IP_PI> "chmod 600 ~/BTL_IOT/.env"
```

Lộ token thì `/revoke` trong BotFather; lộ key Gemini thì xoá key trong AI Studio.
Thiếu `MQTT_PASSWORD` thì collector bỏ qua MQTT nhưng vẫn thu dữ liệu. Thiếu token hoặc
`ALLOWED_CHAT_IDS` thì bot không chạy (thoát mã 2).

### 3.2. `config.yaml`

Mọi tham số nằm ở `config.yaml` (đọc bằng `aiot.config.load_config()`):

| Mục | Hay sửa |
| --- | ------- |
| `mqtt.host` | IP laptop chạy Mosquitto (đổi khi đổi mạng; xem `ipconfig`) |
| `control` | ngưỡng quạt: vào BẢO VỆ > 55 µg/m³, thoát < 35, khẩn cấp ≥ 150; `demo: true` rút thời gian chờ còn 20 s |
| `telegram` | giờ yên lặng mặc định 22–6, cooldown cảnh báo 30 phút |
| `gemini` | tên model (`gemini-3.5-flash-lite`), tối đa 3 lần gọi/ngày |
| `weather` | ngưỡng nắng (> 30000 lux và ≥ 33 °C), mưa (≥ 85% và < 5000 lux, 8h–16h) |

Sửa `config.yaml` xong thì khởi động lại dịch vụ liên quan (`sudo systemctl restart aiot-...`).

---

## 4. Kiểm tra phần cứng

Chạy trên Pi, có người đứng cạnh mạch (phần quạt và đèn MQ135 hỏi người trả lời):

```bash
ssh -t admin@<IP_PI> "cd ~/BTL_IOT && venv/bin/python test_all.py"
i2cdetect -y 1                                  # phải thấy 23 (BH1750)
venv/bin/python -m aiot.camera /tmp/thu.jpg && echo "camera ok"
```

`test_all.py` kiểm tra DHT11, PMS7003, BH1750, MQ135 DO và relay + quạt. Không có TTY thì
script bỏ qua phần quạt, không đụng relay, mã thoát 1. Không chạy `test_all.py` cùng lúc với
`aiot-collector` hoặc `aiot-controller` (dùng chung cảm biến và GPIO23).

---

## 5. Cài và bật các dịch vụ trên Pi

Làm theo thứ tự. Mỗi bước có lệnh kiểm tra; đạt rồi mới sang bước sau. Các lệnh `sudo` hỏi
mật khẩu của user `admin`.

### 5.1. Thu thập dữ liệu: `aiot-collector`

```bash
cd ~/BTL_IOT
timedatectl show -p NTPSynchronized --value     # nên là yes
pkill -f pi_sensor_client.py; pgrep -af pi_sensor_client.py   # client cũ phải tắt (dùng chung cảm biến)
venv/bin/python -m aiot.collector               # chạy thử: thấy "T=... H=... Lux=... PM2.5=..." mỗi 10 s, Ctrl+C để dừng
sudo cp deploy/aiot-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiot-collector
journalctl -u aiot-collector -f
```

- MQ135 hiện `None` trong 3 phút đầu sau mỗi lần collector khởi động (cảm biến cần sấy), rồi
  mới đọc `0` (vượt ngưỡng, đèn DO sáng) hoặc `1`.
- Cửa sổ 5 phút đầu tiên thường `valid=0` vì collector bắt đầu giữa chừng.
- Sau 1 giờ kiểm tra:
  `sqlite3 ~/iot_data/aiot.db "select count(*), sum(valid) from readings;"` (khoảng 12 dòng, phần lớn hợp lệ).

### 5.2. Dự báo: `aiot-forecast.timer`

```bash
venv/bin/python -m aiot.forecast                # chạy thử; chưa đủ dữ liệu thì log "không dự báo" (bình thường)
sudo cp deploy/aiot-forecast.service deploy/aiot-forecast.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiot-forecast.timer
systemctl list-timers aiot-forecast.timer
```

Dùng `persistence` khi chưa đủ 24 giờ dữ liệu, `seasonal_naive` khi đủ, `lstm` khi đã chép
mô hình lên Pi (mục 8).

### 5.3. Điều khiển quạt: `aiot-controller`

**Bước 1 — demo với DB thử** (có người đứng cạnh quạt, không dùng DB thật):

```bash
cd ~/BTL_IOT
sed -e 's#^db_path:.*#db_path: /home/admin/iot_data/demo.db#' -e 's#^  demo: false#  demo: true#' config.yaml > /tmp/demo.yaml
# Terminal 1 (Ctrl+C để dừng, quạt tắt):
venv/bin/python -m aiot.controller --config /tmp/demo.yaml
# Terminal 2: bơm PM2.5 giả mỗi 10 s
venv/bin/python scripts/inject_pm.py --db ~/iot_data/demo.db --profile high     # 80: quạt quay trong ~1 phút
venv/bin/python scripts/inject_pm.py --db ~/iot_data/demo.db --profile low      # 20: quạt dừng sau ~1 phút
venv/bin/python scripts/inject_pm.py --db ~/iot_data/demo.db --profile danger   # 200: quạt quay ngay
vcgencmd get_throttled                          # khi quạt chạy phải là throttled=0x0
```

**Bước 2 — bật dịch vụ thật** (`demo: false` trong `config.yaml`):

```bash
sudo cp deploy/aiot-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiot-controller
journalctl -u aiot-controller -f                # thấy "Controller bắt đầu: ... mode=NORMAL"
```

Luật: vào BẢO VỆ (quạt bật) khi PM2.5 TB 10 phút > 55 liên tục ≥ 5 phút; thoát khi TB 15
phút < 35 liên tục ≥ 15 phút và đã bật ≥ 10 phút; trung vị 1 phút ≥ 150 thì bật ngay; dự báo
TB 3 giờ tới > 100 thì bật trước. Mất dữ liệu PM > 2 phút thì giữ nguyên trạng thái và cảnh
báo. Dừng dịch vụ thì quạt tắt; khởi động lại thì khôi phục trạng thái cuối.

### 5.4. Bot Telegram: `aiot-bot`

Cần `.env` có `TELEGRAM_TOKEN` và `ALLOWED_CHAT_IDS` (mục 3).

```bash
sudo cp deploy/aiot-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiot-bot
journalctl -u aiot-bot -f                       # thấy "Bot bắt đầu: ... chat được phép" và "Đã đặt menu lệnh"
```

Gửi `/help` cho bot để thử. Từ tài khoản không có trong `ALLOWED_CHAT_IDS`, bot im lặng.

### 5.5. Báo cáo sáng: `aiot-morning.timer`

Cần `GEMINI_API_KEY` trong `.env` (thiếu thì vẫn gửi, dùng lời khuyên mẫu).

```bash
sudo cp deploy/aiot-morning.service deploy/aiot-morning.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiot-morning.timer
systemctl list-timers aiot-morning.timer        # lần chạy kế tiếp 06:30
```

Không chạy thử `python -m aiot.report` trong ngày nếu không cần: nó đánh dấu đã gửi báo cáo
hôm nay nên 06:30 sẽ không gửi lại. Đổi giờ báo cáo: sửa `OnCalendar` trong
`deploy/aiot-morning.timer` **và** `report.time` trong `config.yaml`.

### 5.6. Cảnh báo nắng/mưa kèm ảnh: `aiot-weather.timer`

DHT11, BH1750 và camera đặt ngoài ban công.

```bash
venv/bin/python -m aiot.weather                 # chạy thử một lần, xem log
sudo cp deploy/aiot-weather.service deploy/aiot-weather.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiot-weather.timer
sudo systemctl restart aiot-bot                 # để bot gửi được ảnh và có /photo
systemctl list-timers aiot-weather.timer
```

- Nắng gắt: lux > 30000 và nhiệt độ ≥ 33 °C. Có thể sắp mưa: độ ẩm ≥ 85% và lux < 5000, chỉ 8h–16h.
- Điều kiện phải đúng 15 phút liền; mỗi loại tối đa 1 tin / 3 giờ; theo giờ yên lặng.
- Ảnh lưu ở `~/iot_data/photos/`, tự xoá sau 7 ngày. Đây là ước lượng từ cảm biến, không phải dự báo thời tiết.
- Che bóng cho DHT11 (phơi nắng sẽ đọc cao hơn thật); mạch không chống nước.

### 5.7. Cài tất cả trong một lần (khi đã hiểu các bước trên)

Cách khuyến nghị: `bash ~/BTL_IOT/scripts/pi_up.sh` (tự làm mục 1, 5 và in bảng kiểm tra;
`--no-fan` để chưa bật controller). Lệnh thủ công tương đương:

```bash
ssh -t admin@<IP_PI> "cd ~/BTL_IOT && sudo cp deploy/aiot-*.service deploy/aiot-*.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now aiot-collector aiot-controller aiot-bot aiot-forecast.timer aiot-morning.timer aiot-weather.timer && systemctl --no-pager is-active aiot-collector aiot-controller aiot-bot aiot-forecast.timer aiot-morning.timer aiot-weather.timer"
```

Chỉ bật `aiot-controller` khi đã làm demo ở 5.3.

---

## 6. Dùng hằng ngày

### 6.1. Lệnh bot Telegram

Gõ `/` trong Telegram để thấy menu lệnh.

| Lệnh | Việc |
| ---- | ---- |
| `/now` | số liệu cảm biến mới nhất (PM, nhiệt độ, độ ẩm, ánh sáng, MQ135) |
| `/status` | chế độ, quạt, ghi đè, lỗi cảm biến, tuổi dữ liệu, giờ yên lặng |
| `/forecast` | dự báo PM2.5 6 giờ tới kèm nhãn AQI |
| `/report` | báo cáo 24 giờ qua (tính vào 3 lượt Gemini/ngày) |
| `/photo` | chụp ảnh ban công ngay |
| `/fan on` / `/fan off` | bật/tắt quạt thủ công trong 2 giờ |
| `/mode auto` | quạt về tự động theo PM2.5 |
| `/mode manual` | giữ quạt như hiện tại trong 2 giờ |
| `/quiet 22 6` | đặt giờ yên lặng 22h → 6h (chỉ cảnh báo khẩn cấp được gửi) |
| `/clear` | xoá ngay tin của bot và của bạn trong 48 giờ qua (không hỏi lại) |
| `/help` | danh sách lệnh |

Lệnh điều khiển cũ hơn 2 phút (gửi lúc mất mạng hoặc lúc controller tắt) bị bỏ để quạt không
bật bất ngờ: gửi lại là được. Bot tự gửi: cảnh báo đổi nhóm AQI, mức khẩn cấp, lỗi cảm biến,
báo cáo 06:30, cảnh báo nắng/sắp mưa kèm ảnh.

### 6.2. Xem trạng thái và log

```bash
systemctl --no-pager is-active aiot-collector aiot-controller aiot-bot aiot-forecast.timer aiot-morning.timer aiot-weather.timer
journalctl -u aiot-collector -n 20              # đổi tên dịch vụ để xem log khác
sqlite3 ~/iot_data/aiot.db "select datetime(ts,'unixepoch','localtime'), round(pm25,1), temp, hum, round(lux), mq135_do from raw_readings order by ts desc limit 5;"
sqlite3 ~/iot_data/aiot.db "select datetime(ts,'unixepoch','localtime'), kind, detail from events order by ts desc limit 10;"
```

### 6.3. Cập nhật code

```bash
# lấy code mới (git pull hoặc git archive như 1.2), rồi:
sudo systemctl restart aiot-collector aiot-controller aiot-bot
```

Timer (`forecast`, `morning`, `weather`) tự dùng code mới ở lần chạy kế tiếp.

### 6.4. Dừng

```bash
sudo systemctl stop aiot-controller             # quạt tắt
sudo systemctl disable --now aiot-weather.timer # tắt cảnh báo nắng/mưa
```

---

## 7. Dashboard và camera trên laptop (tuỳ chọn)

Dashboard Streamlit nhận dữ liệu qua MQTT, lưu thêm một bản CSV ở `C:\mqtt\data\sensors.csv`.

1. Bật broker (giữ cửa sổ này mở):
   ```powershell
   & "C:\Program Files\Mosquitto\mosquitto.exe" -c "C:\Users\<TEN_BAN>\mosq\mosquitto.conf" -v
   ```
2. Trên Pi, `mqtt.host` trong `config.yaml` phải là IP laptop (`ipconfig`), `.env` có
   `MQTT_PASSWORD`; khởi động lại collector sau khi sửa.
3. Bật dashboard rồi mở <http://localhost:8501> (phải mở tab thì dashboard mới nhận dữ liệu):
   ```powershell
   .venv\Scripts\python -m streamlit run server_app.py
   ```

Client camera cũ (gửi ảnh qua MQTT): `venv/bin/python pi_camera_client.py` hoặc
`pi_detection_client.py` (nhận diện người YOLOv8n). Chúng **chiếm `/dev/video0`**: khi chạy,
`/photo` và cảnh báo nắng/mưa không chụp được ảnh. Chỉ chạy một client camera, và chỉ khi cần.

---

## 8. Phần AI: phân tích, huấn luyện, đưa LSTM lên Pi

Chạy trên laptop:

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt -r training\requirements.txt
.venv\Scripts\python -m training.fetch_public          # tải dữ liệu Open-Meteo Hà Nội (đã có sẵn trong training\data)
.venv\Scripts\jupyter nbconvert --to notebook --execute --inplace training\01_phan_tich_chuoi.ipynb   # trend, ACF, ADF
.venv\Scripts\python -m training.train_lstm            # huấn luyện, ghi models\ (không commit)
.venv\Scripts\jupyter nbconvert --to notebook --execute --inplace training\02_danh_gia.ipynb         # so với đối chứng
```

- Dữ liệu hiện tại là PM2.5 mô hình CAMS + thời tiết ERA5 của Hà Nội (nguồn trong
  `training/data/SOURCE.md`), không phải số đo tại trạm.
- Kết quả (07/11/2026): LSTM có MAE thấp hơn persistence 22–31% ở mọi horizon, nhỉnh hơn hồi quy
  Ridge một chút.
- Huấn luyện bằng dữ liệu của Pi: xuất bằng
  `python training/export_hourly.py --db <bản sao aiot.db> --out hourly.csv` (sao DB trên Pi bằng
  `sqlite3 ~/iot_data/aiot.db ".backup /tmp/aiot_copy.db"` rồi `scp`), sau đó
  `python -m training.train_lstm --data hourly.csv`.

**Quy tắc:** chỉ chép mô hình lên Pi khi nó vượt persistence ở horizon ≥ 2 giờ **trên dữ liệu
của Pi**. Khi đạt:

```bash
ssh admin@<IP_PI> "cd ~/BTL_IOT && mkdir -p models && venv/bin/pip install ai-edge-litert"
scp models/pm25_lstm.tflite models/scaler.json admin@<IP_PI>:~/BTL_IOT/models/
ssh admin@<IP_PI> "cd ~/BTL_IOT && venv/bin/python -m aiot.forecast"   # log có "Đã nạp mô hình học máy"
```

Xoá file mô hình thì dự báo tự quay về mô hình đối chứng.

---

## 9. Nghiệm thu 7 ngày

Sau ít nhất 7 ngày chạy liên tục:

```bash
cd ~/BTL_IOT
venv/bin/python scripts/acceptance_report.py --start <YYYY-MM-DD> --end <YYYY-MM-DD>
```

Script in tỉ lệ dữ liệu hợp lệ, thời gian phản ứng của quạt, số lần vi phạm luật giữ trạng
thái, báo cáo sáng đúng giờ, hiệu quả lọc (thêm `--public-csv` để tính tương quan với trạm công
khai). Ghi kết quả và cách đo vào `docs/nghiem_thu.md`. Thử khói chỉ dùng lượng nhỏ, nơi thoáng,
có người trông; quạt mini là mô phỏng (proof of concept).

---

## 10. Xử lý sự cố

| Triệu chứng | Nguyên nhân | Cách sửa |
| ----------- | ----------- | -------- |
| `/fan on` mà quạt không quay | `aiot-controller` chưa cài hoặc đã dừng (`/status` cảnh báo controller không chạy) | cài/bật ở 5.3; gửi lại `/fan on` vì lệnh cũ hơn 2 phút bị bỏ |
| `/fan on` bị trả lời "đồng hồ chưa đồng bộ" | Pi vừa khởi động, chưa có mạng (không có RTC) | chờ có mạng; `timedatectl` |
| MQ135 hiện "chưa có" | 3 phút sấy sau khi collector khởi động | chờ 3 phút |
| MQ135 luôn `0` | đèn DO sáng: ngưỡng biến trở quá thấp | vặn biến trở tới khi đèn vừa tắt trong không khí sạch |
| MQ135 luôn `1` dù đèn DO sáng | mất GND của module | kiểm tra dây GND, đèn nguồn |
| `/photo` "Camera không phản hồi" | camera bận (client camera cũ đang chạy) hoặc rút cáp | tắt `pi_camera_client.py`/`pi_detection_client.py`; `ls /dev/video0` |
| `/clear` không xoá tin cũ | Telegram chỉ cho bot xoá tin gửi chưa quá 48 giờ | dùng mục Xoá lịch sử của Telegram cho tin cũ hơn |
| Bot không trả lời | `aiot-bot` dừng, thiếu token/chat id, hoặc chat không có trong `ALLOWED_CHAT_IDS` | `journalctl -u aiot-bot -n 30` |
| Báo cáo luôn ghi "(lời khuyên mẫu)" | thiếu `GEMINI_API_KEY`, sai key, hết lượt, thiếu `google-genai` | kiểm tra `.env`, `venv/bin/pip install google-genai` |
| `Checksum did not validate` liên tục | thiếu `libgpiod2`/`sysv-ipc`, dây DHT11 lỏng | cài gói ở 1.1, cắm lại dây |
| `i2cdetect -y 1` không thấy `23` | dây SDA/SCL, rail breadboard bị ngắt | dây nguồn BH1750 cùng đoạn rail với DHT11 |
| Không có PM2.5 | UART chưa bật hoặc serial console còn chiếm | `ls -l /dev/serial0`; làm lại 1.1 |
| Dashboard "Mất kết nối" | broker chưa chạy, sai mật khẩu, sai `mqtt.host` | mục 7; `Get-NetTCPConnection -LocalPort 1883` |
| `timed out` từ Pi tới broker | Windows Firewall chặn hoặc khác mạng | cho phép `mosquitto.exe`, cùng Wi-Fi |
| Sụt áp khi quạt chạy (`throttled` khác `0x0`) | quạt lấy 5V từ Pi | dùng nguồn 5V riêng, nối chung GND |
| `ModuleNotFoundError` | chạy ngoài venv | dùng `venv/bin/python` |

---

## Phụ lục

### Phụ lục A — Topic MQTT

| Topic | Nội dung |
| ----- | -------- |
| `nha/pi01/sensor` | JSON cảm biến mỗi 10 s |
| `nha/pi01/sensor_status` | `online` / `offline` (collector, retained) |
| `nha/pi01/image` | ảnh JPEG (client camera cũ) |
| `nha/pi01/detection` | JSON nhận diện người |
| `nha/pi01/status` | `online` / `offline` (client camera, retained) |

```json
{"timestamp": "2026-11-05T08:00:00", "device_id": "pi01",
 "temperature_c": 28.5, "humidity_pct": 83.0, "lux": 170.8,
 "pm1": 26.0, "pm25": 39.0, "pm10": 47.0, "mq135_do": 1}
```

### Phụ lục B — Dữ liệu sinh ra

| Dữ liệu | Vị trí |
| ------- | ------ |
| SQLite (nguồn chính) | Pi: `/home/admin/iot_data/aiot.db` — bảng `raw_readings`, `readings`, `forecasts`, `events`, `state`, `outbox` (lược đồ trong `CLAUDE.md`) |
| CSV 5 cột (định dạng cũ) | Pi: `/home/admin/iot_data/sensors.csv`; laptop: `C:\mqtt\data\sensors.csv` |
| Ảnh | Pi: `/home/admin/iot_data/photos/` (xoá sau 7 ngày) |
| Mô hình | laptop: `models/` (không commit); Pi: `~/BTL_IOT/models/` khi đã triển khai |

CSV: `timestamp,device_id,temperature_c,humidity_pct,lux`, mỗi 10 s, header một lần, không có dòng rỗng.

### Phụ lục C — Bẫy `lgpio` (Python 3.13)

Wheel `lgpio` trên PyPI chỉ có cho cp39–cp312, Raspberry Pi OS mới dùng Python 3.13 nên pip
phải build từ source.

| Lỗi | Nguyên nhân |
| --- | ----------- |
| `command 'swig' failed` | thiếu `swig` |
| `cannot find -llgpio` | thiếu thư viện C `liblgpio` |
| cài xong mà `import lgpio` lỗi | pip lấy nhầm wheel rỗng `lgpio-0.0.0.2` từ piwheels |

Cách 1 (khuyến nghị) — wheel của Adafruit:

```bash
venv/bin/pip install https://github.com/adafruit/lgpio-python-wheels/raw/main/wheels/lgpio-0.2.2.0-cp313-cp313-linux_aarch64.whl
```

Cách 2 — build: `sudo apt install -y swig build-essential python3-dev liblgpio-dev` rồi cài lại requirements.
Cách 3 — dùng bản của hệ điều hành: `sudo apt install -y python3-lgpio`, tạo lại venv với `--system-site-packages`.
Kiểm tra: `venv/bin/python -c "import lgpio; print(lgpio.__version__)"` phải ra `0.2.2.0`.

### Phụ lục D — Broker không đọc được file `passwd`

Log `password-file: Error: Unable to open pwfile "C:\Program Files\Mosquitto\passwd"` là lỗi
quyền file, không phải sai mật khẩu. Giữ `passwd` và `mosquitto.conf` trong
`C:\Users\<TEN_BAN>\mosq\` như mục 2.2; file đã bị chặn quyền thì
`icacls "C:\Users\<TEN_BAN>\mosq\passwd" /reset`.

### Phụ lục E — Cấu trúc thư mục

| Đường dẫn | Vai trò |
| --------- | ------- |
| `aiot/` | các dịch vụ trên Pi: `collector`, `controller`, `bot`, `forecast`, `report`, `weather` cùng các module dùng chung (`config`, `db`, `sensors`, `pms7003`, `control`, `relay`, `aqi`, `notifier`, `telegram_api`, `advice`, `camera`, `payload`, `aggregate`) |
| `deploy/` | unit systemd |
| `scripts/` | `inject_pm.py` (demo quạt), `acceptance_report.py` (nghiệm thu) |
| `training/` | dữ liệu công khai, phân tích Chương 12, huấn luyện và đánh giá LSTM |
| `tests/` | test chạy trên laptop (`.venv\Scripts\python -m pytest`) |
| `config.yaml`, `.env.example` | cấu hình; mẫu file bí mật |
| `test_all.py` | kiểm tra phần cứng trên Pi |
| `pi_sensor_client.py` | client cũ (DHT11 + BH1750), đã được `aiot-collector` thay thế; không chạy cùng |
| `pi_camera_client.py`, `pi_detection_client.py`, `model.pt` | client camera/nhận diện người qua MQTT (tuỳ chọn) |
| `server_app.py` | dashboard Streamlit trên laptop |
| `docs/` | plan triển khai, bảng nghiệm thu |
