# Vận hành chi tiết — trạm quan trắc không khí AIoT

Các bước thủ công đầy đủ. Cách nhanh (một lệnh) nằm ở [README](../README.md#chạy-nhanh):
`scripts/pi_up.sh` cài, bật và kiểm tra các dịch vụ ngay trên Pi. Có thể gọi qua SSH bằng `scripts/up.ps1`.
Đọc tài liệu này khi cần hiểu từng bước, chạy demo quạt (5.3), xuất dữ liệu và huấn luyện bộ nhận diện nguồn bụi (8) hoặc gỡ lỗi (10).

Tài liệu liên quan: cách nối dây ở [so_do_noi_day_raspberry_pi.md](../so_do_noi_day_raspberry_pi.md),
quy tắc dự án ở [CLAUDE.md](../CLAUDE.md), bảng nghiệm thu ở [nghiem_thu.md](nghiem_thu.md).
---

## Mục lục

0. [Tổng quan](#0-tổng-quan)
1. [Chuẩn bị Raspberry Pi (một lần)](#1-chuẩn-bị-raspberry-pi-một-lần)
2. [Môi trường phát triển (tuỳ chọn)](#2-môi-trường-phát-triển-tuỳ-chọn)
3. [Bí mật và cấu hình](#3-bí-mật-và-cấu-hình)
4. [Kiểm tra phần cứng](#4-kiểm-tra-phần-cứng)
5. [Cài và bật các dịch vụ trên Pi](#5-cài-và-bật-các-dịch-vụ-trên-pi)
6. [Dùng hằng ngày](#6-dùng-hằng-ngày)
7. [Web Render](#7-web-render)
8. [Xuất dữ liệu và huấn luyện bộ nhận diện nguồn bụi](#8-xuất-dữ-liệu-và-huấn-luyện-bộ-nhận-diện-nguồn-bụi)
9. [Nghiệm thu 7 ngày](#9-nghiệm-thu-7-ngày)
10. [Xử lý sự cố](#10-xử-lý-sự-cố)
- [Phụ lục](#phụ-lục)

---

## 0. Tổng quan

### Luồng hoạt động

```text
Cảm biến → aiot-collector → SQLite và CSV trên Pi
SQLite ↔ aiot-controller → relay quạt (GPIO23) và còi (GPIO5)
SQLite ↔ aiot-forecast
SQLite ↔ aiot-uplink ↔ HTTPS ↔ Web Render ↔ Neon Postgres
```

- **Local-first:** đo, lưu, dự báo và điều khiển chạy trên Pi. Mất mạng vẫn đo, vẫn điều khiển;
  uplink gửi bù số liệu lên web khi có mạng lại.
- Controller dùng nhãn nguồn bụi hoặc luật ngưỡng PM2.5 để điều khiển tại Pi.
- Các dịch vụ trên Pi trao đổi qua SQLite. Controller điều khiển GPIO23 và GPIO5.
- Không gửi tin cảnh báo: mọi sự kiện (`fan_on`, `source_change`, `sensor_fault`, `relay_error`...)
  ghi vào bảng `events` và hiện ở nhật ký trên web; còi vẫn kêu tại chỗ. Điều khiển thủ công chỉ qua web.
- Bot Telegram, báo cáo sáng (Gemini) và cảnh báo nắng/mưa kèm ảnh camera đã gỡ ngày 29/11/2026.

### Các dịch vụ trên Pi

| Dịch vụ (systemd) | Chạy | Việc |
| ----------------- | ---- | ---- |
| `aiot-collector` | liên tục | mẫu 1 s vào SQLite, mẫu 10 s vào SQLite và CSV, gộp theo cửa sổ 5 phút |
| `aiot-controller` | liên tục | mỗi 5 giây xét AI, luật PM2.5 và lệnh điều khiển |
| `aiot-uplink` | mỗi 5 giây khi có cấu hình | gửi dữ liệu và nhận lệnh từ Render qua HTTPS |
| `aiot-forecast.timer` | mỗi giờ, phút 02 | dự báo PM2.5 6 giờ tới |

### Phần cứng

Raspberry Pi 4 Model B 4 GB. Số chân là số vật lý trên header 40 chân.

| Thiết bị | Dữ liệu | Kết nối |
| -------- | ------- | ------- |
| DHT11 | nhiệt độ, độ ẩm | OUT → pin 11 (GPIO17) |
| BH1750 | ánh sáng (lux) | I2C pin 3, 5; địa chỉ `0x23` |
| PMS7003 | PM1.0, PM2.5, PM10 | UART pin 8, 10 (`/dev/serial0`); 5V |
| Relay + quạt 5V | mô phỏng máy lọc | IN → pin 16 (GPIO23); HIGH = quạt quay |
| Còi chủ động | báo vượt ngưỡng tại chỗ | I/O → pin 29 (GPIO5), VCC pin 17 (3.3V), GND pin 25; LOW = kêu |

**Tắt Pi và rút cáp nguồn trước khi đổi dây.** Chi tiết dây, rail breadboard và các lỗi hay
gặp xem `so_do_noi_day_raspberry_pi.md`.

---

## 1. Chuẩn bị Raspberry Pi (một lần)

Pi dùng user `admin`, code ở `~/BTL_IOT`, venv ở `~/BTL_IOT/venv`, dữ liệu ở `~/iot_data`.
Trong các lệnh SSH, `<IP_PI>` là địa chỉ hoặc tên Pi mà máy triển khai truy cập được, ví dụ `pi.local`.

### 1.1. Gói hệ thống, I2C, UART, múi giờ

```bash
sudo apt update
sudo apt install -y libgpiod2 sysv-ipc i2c-tools python3-venv sqlite3
sudo raspi-config nonint do_i2c 0            # bật I2C (BH1750)
sudo raspi-config nonint do_serial_hw 0      # bật UART phần cứng (PMS7003)
sudo raspi-config nonint do_serial_cons 1    # tắt serial console trên UART
sudo timedatectl set-timezone Asia/Ho_Chi_Minh
sudo usermod -aG gpio,i2c,dialout admin
sudo reboot
```

- `libgpiod2` + `sysv-ipc` bắt buộc cho DHT11; thiếu thì lỗi `Checksum did not validate` liên tục.
- Múi giờ phải là `Asia/Ho_Chi_Minh`: CSV và `acceptance_report.py` (`--start`, `--end`) dùng giờ
  địa phương.

### 1.2. Đưa code lên Pi

Khi nhánh đã có trên GitHub:

```bash
git clone <URL_GITHUB> ~/BTL_IOT          # lần đầu
cd ~/BTL_IOT && git pull                  # các lần sau
```

Khi nhánh chưa push (đang làm trên laptop), chép từ laptop bằng Git Bash ở thư mục repo:

```bash
git archive HEAD aiot deploy scripts config.yaml requirements-pi.txt test_all.py README.md | ssh admin@<IP_PI> "cd ~/BTL_IOT && tar -xf -"
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
  vẫn lỗi) xem [Phụ lục B](#phụ-lục-b--bẫy-lgpio-python-313).
- `requirements-pi.txt` chỉ có thư viện lõi.

Kiểm tra nhanh:

```bash
venv/bin/python -c "import aiot.collector, aiot.controller, aiot.forecast, aiot.uplink; print('ok')"
```

---

## 2. Môi trường phát triển (tuỳ chọn)

Dùng máy phát triển để chạy kiểm thử, mô phỏng cloud hoặc huấn luyện.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

---

## 3. Bí mật và cấu hình

### 3.1. File `.env` (không commit)

Chép `.env.example` thành `.env` ở thư mục repo, điền:

| Khoá | Lấy ở đâu |
| ---- | --------- |
| `INGEST_TOKEN` | cùng giá trị với biến môi trường `INGEST_TOKEN` trên Render |

Chép `.env` lên Pi và khoá quyền đọc:

```bash
scp .env admin@<IP_PI>:~/BTL_IOT/.env
ssh admin@<IP_PI> "chmod 600 ~/BTL_IOT/.env"
```

Uplink cần `INGEST_TOKEN` cùng cấu hình `cloud` trên Pi. File `.env` cũ còn `TELEGRAM_TOKEN`,
`ALLOWED_CHAT_IDS`, `GEMINI_API_KEY` thì xoá các dòng đó.

### 3.2. `config.yaml`

Mọi tham số nằm ở `config.yaml` (đọc bằng `aiot.config.load_config()`):

| Mục | Hay sửa |
| --- | ------- |
| `cloud` | bật bằng `enabled: true`, đặt `url` thành địa chỉ HTTPS của web Render |
| `control` | ngưỡng quạt: vào BẢO VỆ > 55 µg/m³, thoát < 35, khẩn cấp ≥ 150; ghi đè thủ công 2 giờ; `stale_command_s` (120) bỏ lệnh web chờ quá lâu; `demo: true` rút thời gian chờ còn 20 s |
| `buzzer` | còi kêu khi trung vị PM2.5 1 phút ≥ `alarm_pm25` (150); bật thủ công từ web tối đa `manual_max_s` (60 s) |

Sửa `config.yaml` xong thì khởi động lại dịch vụ liên quan (`sudo systemctl restart aiot-...`).

---

## 4. Kiểm tra phần cứng

Chạy trên Pi, có người đứng cạnh mạch (phần quạt và còi hỏi người trả lời). Dừng dịch vụ trước
bằng `bash scripts/pi_up.sh --stop`, test xong bật lại bằng `bash scripts/pi_up.sh`:

```bash
ssh -t admin@<IP_PI> "cd ~/BTL_IOT && venv/bin/python test_all.py"
i2cdetect -y 1                                  # phải thấy 23 (BH1750)
```

`test_all.py` kiểm tra DHT11, PMS7003, BH1750, relay + quạt và còi. Không có TTY thì
script bỏ qua phần quạt và còi, không đụng relay, mã thoát 1. Khi `aiot-collector` hoặc
`aiot-controller` đang chạy (dùng chung cảm biến, GPIO23, GPIO5), script tự thoát và nhắc dừng
dịch vụ. Còi kêu không dứt sau khi thử: `pinctrl set 5 op dh` đưa GPIO5 về mức im.

---

## 5. Cài và bật các dịch vụ trên Pi

Làm theo thứ tự. Mỗi bước có lệnh kiểm tra; đạt rồi mới sang bước sau. Các lệnh `sudo` hỏi
mật khẩu của user `admin`.

### 5.1. Thu thập dữ liệu: `aiot-collector`

```bash
cd ~/BTL_IOT
timedatectl show -p NTPSynchronized --value     # nên là yes
venv/bin/python -m aiot.collector               # chạy thử: thấy "T=... H=... Lux=... PM2.5=..." mỗi 10 s, Ctrl+C để dừng
sudo cp deploy/aiot-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aiot-collector
journalctl -u aiot-collector -f
```

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

Pi dự báo PM2.5 cho 6 giờ tới bằng `seasonal_naive` nếu đủ dữ liệu theo giờ. Khi thiếu
dữ liệu, `persistence` lặp lại mức PM2.5 của giờ hợp lệ gần nhất.

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
TB 3 giờ tới > 100 thì bật trước. Mất dữ liệu PM > 2 phút thì giữ nguyên trạng thái và ghi sự
kiện `sensor_fault`. Dừng dịch vụ thì quạt tắt; khởi động lại thì khôi phục trạng thái cuối.

Bot Telegram (`aiot-bot`), báo cáo sáng (`aiot-morning.timer`) và cảnh báo nắng/mưa kèm ảnh
(`aiot-weather.timer`) đã gỡ ngày 29/11/2026. `pi_up.sh` tự dừng và xoá các unit này nếu Pi còn.

### 5.4. Cài tất cả trong một lần (khi đã hiểu các bước trên)

Cách khuyến nghị là chạy `bash ~/BTL_IOT/scripts/pi_up.sh` trên Pi. Script chuẩn bị môi trường,
bật dịch vụ và in bảng kiểm tra. Thêm `--no-fan` nếu chưa bật controller.

Với web Render, chuẩn bị cấu hình ở mục 7 để script bật `aiot-uplink`.

---

## 6. Dùng hằng ngày

### 6.1. Điều khiển trên web

Mở [web của trạm](https://aiot-tram.onrender.com/), đăng nhập bằng mật khẩu nhóm.

| Thao tác | Việc |
| -------- | ---- |
| Tự động | quạt và còi theo nhãn AI hoặc luật PM2.5; xoá ghi đè quạt và còi |
| Thủ công | giữ quạt như hiện tại trong 2 giờ |
| Bật / tắt quạt | ghi đè quạt trong 2 giờ (chỉ bấm được ở chế độ thủ công) |
| Bật / tắt còi | bật kêu tối đa 60 s rồi tự tắt; tắt thì giữ im |

Lệnh quá 60 s chưa được Pi nhận thì web ghi "hết hạn"; lệnh Pi đã nhận nhưng chờ quá
`control.stale_command_s` (120 s) mà controller chưa áp dụng (ví dụ controller đang dừng) thì bị bỏ
để quạt không bật bất ngờ: bấm lại là được. Không có tin cảnh báo tự gửi: xem nhật ký trên web
hoặc bảng `events` (mục 6.2).

### 6.2. Xem trạng thái và log

```bash
bash ~/BTL_IOT/scripts/pi_up.sh --status
journalctl -u aiot-collector -n 20              # đổi tên dịch vụ để xem log khác
sqlite3 ~/iot_data/aiot.db "select datetime(ts,'unixepoch','localtime'), round(pm25,1), temp, hum, round(lux) from raw_readings order by ts desc limit 5;"
sqlite3 ~/iot_data/aiot.db "select datetime(ts,'unixepoch','localtime'), kind, detail from events order by ts desc limit 10;"
```

### 6.3. Cập nhật code

```bash
# Lấy code mới theo mục 1.2 rồi cập nhật dịch vụ trên Pi.
bash ~/BTL_IOT/scripts/pi_up.sh
```

Timer `aiot-forecast` tự dùng code mới ở lần chạy kế tiếp.

### 6.4. Dừng

```bash
sudo systemctl stop aiot-controller             # quạt tắt
```

---

## 7. Web Render

Mở [web của trạm](https://aiot-tram.onrender.com/) để xem số liệu, AQI, nhãn nguồn bụi,
biểu đồ 6 giờ vừa qua và nhật ký. Đăng nhập bằng mật khẩu nhóm để chuyển chế độ hoặc điều khiển quạt và còi.
Trên Pi, `pi_up.sh` bật `aiot-uplink` khi đã bật cloud và đặt URL cùng `INGEST_TOKEN`.
Xem cách cấu hình Render và Neon trong [hướng dẫn triển khai](../README.md#deploy-một-lần-người-trong-nhóm-làm).

Khi cập nhật một Pi đã chạy bản cũ, dừng các client cảm biến cũ nếu chúng vẫn chạy
để giải phóng phần cứng. Bản mới bỏ qua mục `mqtt` trong cấu hình cũ, nên có thể xoá mục đó
và khoá `MQTT_PASSWORD` khỏi cấu hình triển khai.

---

## 8. Xuất dữ liệu và huấn luyện bộ nhận diện nguồn bụi

Cách xuất mẫu, sinh dữ liệu và huấn luyện logistic regression được mô tả trong [hướng dẫn nhận diện nguồn bụi](nhan_dien_nguon_bui.md).
Để đối chiếu dự báo với số đo theo giờ, chạy lệnh sau với bản sao SQLite của trạm.

```powershell
.venv\Scripts\python -m training.export_hourly --db .\aiot.db --out .\hourly.csv
```

---

## 9. Nghiệm thu 7 ngày

Sau ít nhất 7 ngày chạy liên tục:

```bash
cd ~/BTL_IOT
venv/bin/python scripts/acceptance_report.py --start <YYYY-MM-DD> --end <YYYY-MM-DD>
```

Script in tỉ lệ dữ liệu hợp lệ, thời gian phản ứng của quạt, số lần vi phạm luật giữ trạng
thái, hiệu quả lọc (thêm `--public-csv` để tính tương quan với trạm công
khai). Ghi kết quả và cách đo vào `docs/nghiem_thu.md`. Thử khói chỉ dùng lượng nhỏ, nơi thoáng,
có người trông; quạt mini là mô phỏng (proof of concept).

---

## 10. Xử lý sự cố

| Triệu chứng | Nguyên nhân | Cách sửa |
| ----------- | ----------- | -------- |
| Bấm "Bật quạt" trên web mà quạt không quay | `aiot-controller` chưa cài hoặc đã dừng (`pi_up.sh --status` báo controller không chạy) | cài/bật ở 5.3; bấm lại vì lệnh chờ quá 120 s bị bỏ |
| Còi kêu không dứt | GPIO5 bị để ở LOW (script thử còi dừng giữa chừng) | `pinctrl set 5 op dh`; controller chạy lại sẽ tự giữ GPIO5 ở HIGH |
| `Checksum did not validate` liên tục | thiếu `libgpiod2`/`sysv-ipc`, dây DHT11 lỏng | cài gói ở 1.1, cắm lại dây |
| `i2cdetect -y 1` không thấy `23` | dây SDA/SCL, rail breadboard bị ngắt | dây nguồn BH1750 cùng đoạn rail với DHT11 |
| Không có PM2.5 | UART chưa bật hoặc serial console còn chiếm | `ls -l /dev/serial0`; làm lại 1.1 |
| Web báo Pi mất kết nối, lệnh "hết hạn" | uplink dừng, Pi mất Internet hoặc token không khớp | kiểm tra mục "web cloud" trong `pi_up.sh --status` và `journalctl -u aiot-uplink -n 30` |
| Sụt áp khi quạt chạy (`throttled` khác `0x0`) | quạt lấy 5V từ Pi | dùng nguồn 5V riêng, nối chung GND |
| `ModuleNotFoundError` | chạy ngoài venv | dùng `venv/bin/python` |

---

## Phụ lục

### Phụ lục A — Dữ liệu sinh ra

| Dữ liệu | Vị trí |
| ------- | ------ |
| SQLite (nguồn chính) | Pi: `/home/admin/iot_data/aiot.db` — bảng `raw_readings`, `readings`, `forecasts`, `events`, `state` (lược đồ trong `CLAUDE.md`); DB cũ còn bảng `outbox`, `tg_messages` của bot đã gỡ, không dùng nữa |
| CSV 5 cột | Pi tại `/home/admin/iot_data/sensors.csv` |
| Mô hình | laptop: `models/` (không commit); Pi: `~/BTL_IOT/models/` khi đã triển khai |

CSV: `timestamp,device_id,temperature_c,humidity_pct,lux`, mỗi 10 s, header một lần, không có dòng rỗng.

### Phụ lục B — Bẫy `lgpio` (Python 3.13)

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

### Phụ lục C — Cấu trúc thư mục

| Đường dẫn | Vai trò |
| --------- | ------- |
| `aiot/` | các dịch vụ trên Pi và các module dùng chung |
| `cloud/` | web FastAPI trên Render |
| `deploy/` | unit systemd |
| `scripts/` | `pi_up.sh`, `up.ps1` (bật trạm), `inject_pm.py` (demo quạt), `label_event.py` (gắn nhãn), `buzzer_probe.py`, `cloud_demo.py`, `acceptance_report.py` (nghiệm thu) |
| `training/` | xuất dữ liệu trạm, huấn luyện và đánh giá bộ nhận diện nguồn bụi |
| `tests/` | test chạy trên laptop (`.venv\Scripts\python -m pytest`) |
| `config.yaml`, `.env.example` | cấu hình; mẫu file bí mật |
| `test_all.py` | kiểm tra phần cứng trên Pi |
| `docs/` | vận hành, nghiệm thu, kế hoạch web, nhận diện nguồn bụi; `docs/archive/` là tài liệu cũ |
