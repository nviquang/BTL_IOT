# Trạm quan trắc không khí AIoT tại nhà

Bài tập nhóm môn IoT, dựa trên Chương 12 *Hands-On Artificial Intelligence for IoT*. Chuỗi xử lý: thu thập cục bộ → dự báo → Gemini → Telegram → tự động bật/tắt quạt lọc.

Cách nối dây chi tiết và kết quả test nằm ở `so_do_noi_day_raspberry_pi.md`.

Không thuộc hệ thống (đã loại khỏi thiết kế):
- ADS1115 và cảm biến khí MQ135 (chỉ có chân DO 1 bit, không đọc được nồng độ; đã gỡ khỏi mạch và code ngày 23/11/2026)
- Toàn bộ phần đóng/mở cửa: servo, mô hình cửa, công tắc hành trình, lệnh `/door`, lỗi "cửa kẹt"
- DHT22 (mạch dùng DHT11)
- Nguồn 5V rời, OLED, LED báo (không có trong mạch hiện tại)

## Trạng thái dự án

| Phần | Trạng thái |
| ---- | ---------- |
| Mạch: DHT11, PMS7003, BH1750, relay + quạt | Đạt `test_all.py` ngày 04/11/2026 và 23/11/2026 |
| Thu thập DHT11 + BH1750 → CSV + MQTT → dashboard Streamlit | Có code (`pi_sensor_client.py`, `server_app.py`) |
| Camera + nhận diện người YOLOv8n qua MQTT | Có code (`pi_camera_client.py`, `pi_detection_client.py`) |
| Collector `aiot/collector.py`: 4 cảm biến → SQLite + CSV + MQTT, gộp 5 phút | Có code, đã chạy thử 75 s trên Pi ngày 07/11/2026, chưa cài dịch vụ (README 2.4a) |
| Điều khiển quạt tự động `aiot/controller.py` | Có code, chưa thử trên mạch (README 2.4c) |
| Còi báo vượt ngưỡng (`aiot/alarm.py`, `aiot/buzzer.py`, `/mute`) | Có code và test, chưa nối và thử trên mạch |
| Bot Telegram, cảnh báo, báo cáo sáng, lời khuyên Gemini | Có code, chưa thử với bot thật và Gemini thật (README 2.4d, 2.4f) |
| Dự báo PM2.5 mỗi giờ (đối chứng + LSTM) | Có code; LSTM đã huấn luyện/đánh giá trên dữ liệu CAMS công khai, chưa triển khai (README 2.4e, 2.4g) |
| Cảnh báo nắng/mưa kèm ảnh camera (`aiot/weather.py`, `/photo`, menu lệnh bot) | Có code, chưa thử thật (README 2.4h) |
| Nhận diện nguồn bụi AI (`aiot/source_*.py`, `training/train_source.py`): 4 nhãn, mẫu 1 giây, dự đoán mỗi 5 s, quạt + còi theo nhãn | Có code và mô hình huấn luyện trên dữ liệu mô phỏng (hiệu chỉnh theo nhang 23/11); code đã chép lên Pi 23/11/2026 nhưng chưa khởi động lại dịch vụ; chưa có sự kiện nấu ăn thật (`docs/nhan_dien_nguon_bui.md`) |
| Web trên cloud (`cloud/`, `aiot/uplink.py`, `aiot/commands.py`): xem số liệu, nhãn AI, dự báo; Tự động/Thủ công, quạt, còi (cần mật khẩu) | Đã deploy ngày 23/11/2026: https://aiot-tram.onrender.com (Render free + Neon Singapore); Pi chạy `aiot-uplink`, lệnh bật quạt từ web tới controller ~2 s. Chưa thử còi thủ công từ web (README "Web trên cloud", `docs/ke_hoach_web.md`) |
| Nghiệm thu 7 ngày | Có `scripts/acceptance_report.py`; chưa đo, kết quả ghi ở `docs/nghiem_thu.md` |

Việc còn lại theo thứ tự nằm ở mục "Việc cần làm trên Pi" trong `README.md`.

## Phần cứng

Raspberry Pi 4 Model B 4GB. Số chân là số vật lý trên header 40 chân.

| Thiết bị | Dữ liệu | Kết nối chính |
| -------- | ------- | ------------- |
| DHT11 | Nhiệt độ, độ ẩm | OUT → pin 11 (GPIO17) |
| BH1750 | Ánh sáng (lux) | I2C pin 3, 5; địa chỉ `0x23` |
| PMS7003 | PM1.0, PM2.5, PM10 | UART pin 8, 10 (`/dev/serial0`); 5V |
| Relay + quạt 5V | Mô phỏng máy lọc | IN → pin 16 (GPIO23); tải nối COM/NO |
| Còi chủ động (module 3 chân, kích mức thấp) | Báo vượt ngưỡng tại chỗ | I/O → pin 29 (GPIO5); VCC pin 17 (3.3V), GND pin 25 |
| Camera USB | Ảnh | /dev/video0 |

Những điều dễ sai:
- **Còi kích mức thấp:** GPIO5 LOW = kêu, HIGH = im. VCC phải lấy 3.3V (pin 17), cấp 5V thì mức HIGH của Pi không tắt được còi. Phải dùng GPIO 0–8 (mặc định kéo lên) để còi im lúc Pi khởi động; GPIO26 mặc định kéo xuống nên còi sẽ kêu.
- **Relay kích mức HIGH:** GPIO23 HIGH = quạt quay, LOW = quạt dừng. GPIO23 mặc định kéo xuống, nên quạt tắt khi Pi khởi động. Nếu GPIO23 đọc cao khi đang là input, kiểm tra dây GND của relay.
- **Rail breadboard có thể bị ngắt ở giữa:** dây nguồn DHT11 và BH1750 phải cùng đoạn rail trái; VCC của PMS7003 và VCC, COM của relay phải cùng đoạn rail phải.
- **Đầu dây Dupont có thể tiếp xúc kém:** cắm lại chắc trước khi kết luận module hỏng.
- **Quạt đang lấy 5V từ Pi:** chưa thấy sụt áp (`throttled=0x0`). Nếu có sụt áp thì chuyển sang nguồn 5V riêng, nối chung GND với Pi.
- **PMS7003 chưa nối chân SET:** chưa cho cảm biến ngủ theo chu kỳ được, nên laser sẽ hao nhanh hơn nếu đo liên tục lâu dài.

## Kiến trúc mục tiêu

Nguyên tắc **local-first:** đo, lưu, dự báo và điều khiển đều chạy trên Pi, không phụ thuộc Internet. Internet chỉ dùng cho Telegram, Gemini và dữ liệu tham chiếu Open-Meteo. Mất mạng thì vẫn đo, vẫn điều khiển, và gửi bù tin khi có mạng lại.

Nguyên tắc **an toàn AI:** Gemini chỉ viết lời khuyên, không điều khiển thiết bị. Việc bật/tắt quạt là luật tất định chạy cục bộ.

| Việc | Chạy ở đâu |
| ---- | ---------- |
| Đọc cảm biến, lưu dữ liệu, luật điều khiển | Pi |
| Suy luận LSTM (TFLite/LiteRT) | Pi |
| Huấn luyện LSTM | Laptop/Colab, rồi chép `.tflite` sang Pi |
| Viết lời khuyên | Gemini API, khoảng 1–3 lần/ngày |
| Gửi/nhận tin | Telegram long polling, không cần mở port |

## Chức năng dự kiến

- **Thu thập:** gộp trung bình 5 phút, gắn cờ `valid`, loại giá trị lỗi.
- **Báo cáo sáng (mặc định 06:30):** không khí 24 giờ qua, dự báo 6 giờ tới, lời khuyên từ Gemini, trạng thái quạt.
- **Dự báo:** PM2.5 trung bình giờ cho 1–6 giờ tới, kèm nhãn AQI. Đầu vào gồm PM2.5, nhiệt độ, độ ẩm, sin/cos giờ trong ngày, và có thể thêm dự báo gió/mưa từ Open-Meteo.
- **Cảnh báo:** gửi khi đổi nhóm AQI, tối đa 1 tin cùng loại mỗi 30 phút, có giờ yên lặng.
- **Điều khiển:** tự bật quạt khi không khí xấu, tự tắt khi tốt lại, có ghi đè thủ công.

### Nhận diện nguồn bụi (AI điều khiển quạt và còi)

Nhãn và hành động cố định: bình thường → quạt tắt, còi im; nấu ăn → chỉ quạt; ô nhiễm từ ngoài vào (nền PM2.5 ≥ 55,5) → quạt + còi; khói thuốc/nhang → chỉ còi (kể cả khi PM2.5 rất cao, theo yêu cầu nhóm). Ghi đè thủ công, mất dữ liệu > 2 phút, `min_state_s` vẫn áp dụng. Không có mô hình / mẫu 1 giây cũ > 15 s / chưa đủ tin cậy (TB xác suất 3 lần < 0,6) thì dùng luật ngưỡng bên dưới. Sự kiện đốt nhang 21:07 ngày 23/11/2026 (đỉnh 805 và 1061 µg/m³) là dữ liệu thật duy nhất có nhãn `smoking`; nền ~160 tối 23/11 được gán `pollution` (giả định).

### Logic điều khiển quạt (luật ngưỡng, dùng khi AI không có nhãn)

Hai trạng thái: BÌNH THƯỜNG (quạt tắt) và BẢO VỆ (quạt bật). Đưa tham số vào `config.yaml`.

| Tham số | Giá trị đề xuất |
| ------- | --------------- |
| Vào BẢO VỆ | PM2.5 TB 10 phút > 55 µg/m³, liên tục ≥ 5 phút |
| Thoát BẢO VỆ | PM2.5 TB 15 phút < 35 µg/m³, liên tục ≥ 15 phút, và đã giữ trạng thái ≥ 10 phút; không thoát khi còn dự báo mới (≤ 90 phút) với TB 3 giờ tới > 100 µg/m³ |
| Khẩn cấp | Trung vị PM2.5 1 phút (cần ≥ 3 mẫu) ≥ 150 µg/m³: vào ngay, kèm cảnh báo mạnh. Không nhầm với nhóm AQI "Nguy hại" (≥ 225,5) |
| Còi | Trung vị PM2.5 1 phút ≥ `buzzer.alarm_pm25` (mặc định 150): kêu 10 giây, lặp lại mỗi 5 phút khi còn vượt, tắt ngay khi hết; kêu cả giờ yên lặng; `/mute` tắt tiếng |
| Chủ động | Dự báo TB 3 giờ tới > 100 µg/m³: bật quạt trước |
| Chế độ demo | Rút ngắn thời gian chờ, ví dụ 20 giây |

An toàn:
- Ghi đè thủ công có hiệu lực tới `/mode auto` hoặc hết hạn, mặc định 2 giờ.
- Mất dữ liệu cảm biến PM quá 2 phút: giữ nguyên trạng thái, gửi cảnh báo, không quyết định theo số liệu cũ.
- Khởi động lại Pi: đọc trạng thái cuối trong DB rồi đưa quạt về trạng thái hợp lý.
- Quyết định điều khiển không phụ thuộc Gemini hay Internet.

### Telegram

Lệnh: `/now`, `/forecast`, `/report`, `/status`, `/mode auto|manual`, `/fan on|off`, `/quiet 22 6`, `/mute [phút]|off` (tắt tiếng còi), `/buzzer on|off` (còi thủ công, bật tối đa `buzzer.manual_max_s` = 60 s), `/web`. `/mode auto` xoá cả ghi đè quạt lẫn còi.

### Web trên cloud

- Web FastAPI trên Render (`cloud/app.py`, `render.yaml`), dữ liệu ở Neon Postgres. Pi chạy dịch vụ `aiot-uplink` gửi `POST /api/ingest` 5 giây/lần (Bearer `INGEST_TOKEN`) và nhận lệnh về; Pi không mở port.
- Xem công khai; điều khiển cần mật khẩu chung (hash ở biến `WEB_PASSWORD_HASH` trên Render, tạo bằng `python -m cloud.security`).
- Lệnh web và Telegram cùng đi qua `aiot/commands.py` → khoá `pending_override` / `pending_buzzer` → controller (`buzzer_override`). Lệnh quá 60 s chưa được nhận thì hết hạn; mỗi id chỉ áp dụng một lần (khoá `uplink_done`).
- Không gửi lên cloud: ảnh, chat id, IP (uplink bỏ `chat=`, `ip=` khỏi chi tiết sự kiện).

Chỉ phản hồi chat trong `ALLOWED_CHAT_IDS`, không in token vào log, và ghi mọi lệnh điều khiển vào nhật ký.

### Gemini

- Chỉ gửi số liệu đã tổng hợp dạng JSON, không có thông tin cá nhân. Yêu cầu trả về JSON `{headline, advice}`, nhiệt độ khoảng 0.2.
- Yêu cầu không chẩn đoán y tế, không bịa số liệu.
- Lỗi, hết hạn mức hoặc JSON sai: dùng mẫu lời khuyên cố định theo nhóm AQI, bản tin vẫn phải gửi được.
- Tên model đặt trong `config.yaml`, kiểm tra model và hạn mức hiện hành trước khi triển khai.

## Pipeline AI theo Chương 12

1. Vẽ chuỗi giờ và trung bình trượt 24 giờ để xem trend; khử trend bằng `diff()`.
2. Dùng ACF tìm seasonality, kỳ vọng đỉnh tại lag 24; khử bằng sai phân lag 24.
3. Kiểm định ADF trước và sau khi khử.
4. `MinMaxScaler`, **chỉ fit trên tập train**.
5. Cửa sổ trượt: 24 giờ trước → 6 giờ tiếp theo.
6. LSTM 1–2 lớp, 32–64 đơn vị, đầu ra 6 giá trị; xuất `.tflite`.

Yêu cầu về phương pháp:
- Chia dữ liệu theo thời gian, không xáo trộn ngẫu nhiên.
- So với mô hình đối chứng: persistence, seasonal naive, SARIMA hoặc hồi quy trên đặc trưng trễ, và Open-Meteo Air Quality.
- Đánh giá MAE, RMSE theo từng horizon, độ chính xác nhóm AQI, và độ nhạy với sự kiện vượt ngưỡng. Nếu LSTM không hơn đối chứng thì báo cáo trung thực.
- Cold start: thu dữ liệu sớm nhất có thể, huấn luyện sơ bộ bằng dữ liệu công khai của Hà Nội, và dùng mô hình đối chứng khi chưa đủ dữ liệu.

Mức AQI theo PM2.5 (US EPA 2024, µg/m³): Tốt 0–9,0 · Trung bình 9,1–35,4 · Kém 35,5–55,4 · Xấu 55,5–125,4 · Rất xấu 125,5–225,4 · Nguy hại ≥ 225,5.

## Dữ liệu

CSV hiện tại trên cả Pi và laptop có đúng định dạng `timestamp,device_id,temperature_c,humidity_pct,lux`, mỗi 10 giây, một file ghi nối tiếp, header ghi một lần. Mẫu đọc lỗi bị bỏ qua, không ghi dòng rỗng, vì notebook time series không chịu được NaN. Khi sửa client phải giữ định dạng hai bên giống nhau.

- Pi: `/home/admin/iot_data/sensors.csv`
- Laptop: `C:\mqtt\data\sensors.csv`
- Topic MQTT: `nha/pi01/sensor`, `nha/pi01/sensor_status`, `nha/pi01/image`, `nha/pi01/detection`, `nha/pi01/status`

Lược đồ SQLite (`aiot/db.py`, file DB dùng chung cho mọi dịch vụ, bật WAL và `busy_timeout=5000`). Mẫu thô 10 giây vào `raw_readings`, được gộp thành `readings` theo cửa sổ 5 phút căn theo bội số 300 giây (`ts` là đầu cửa sổ):

```sql
CREATE TABLE raw_readings (        -- mẫu thô 10 giây, xoá dần bằng purge_raw
  ts INTEGER PRIMARY KEY,          -- trùng ts thì giữ bản ghi đầu
  pm1 REAL, pm25 REAL, pm10 REAL,
  temp REAL, hum REAL, lux REAL,   -- NULL khi đọc lỗi
  clock_synced INTEGER NOT NULL    -- 0 = đồng hồ chưa đồng bộ NTP
);
CREATE TABLE readings (            -- trung bình 5 phút; cửa sổ không có mẫu thì không có dòng
  ts INTEGER PRIMARY KEY,
  pm1 REAL, pm25 REAL, pm10 REAL,
  temp REAL, hum REAL, lux REAL,
  valid INTEGER DEFAULT 1          -- 1 khi >= nửa số mẫu PM2.5 hợp lệ và đồng hồ đã đồng bộ
);
CREATE TABLE forecasts (
  made_at INTEGER, horizon_h INTEGER, pm25_pred REAL,
  model TEXT NOT NULL,             -- 'lstm', 'persistence', ...
  PRIMARY KEY (made_at, horizon_h)
);
CREATE TABLE events (              -- 'fan_on', 'fan_off', 'override', 'sensor_fault'...
  ts INTEGER, kind TEXT, detail TEXT
);                                 -- có chỉ mục theo ts
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE fast_readings (       -- mẫu 1 giây cho AI nguồn bụi, giữ 48 h (đoạn có nhãn giữ lại)
  ts INTEGER PRIMARY KEY, pm1, pm25, pm10, n03, n05, n10, n25, n50, n100 REAL,
  temp, hum, lux REAL, clock_synced INTEGER NOT NULL
);
CREATE TABLE source_labels (id, start_ts, end_ts, label, note);  -- scripts/label_event.py
CREATE TABLE source_predictions (ts PRIMARY KEY, label, raw_label, proba, model);
-- Khoá state cho lệnh: pending_override (quạt), pending_buzzer -> buzzer_override (còi),
-- buzzer_mute_until (/mute); uplink: uplink_cursor, uplink_done, uplink_last_ok
CREATE TABLE outbox (              -- tin Telegram chờ gửi, gửi bù khi có mạng
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at INTEGER NOT NULL, kind TEXT NOT NULL,
  text TEXT NOT NULL, sent_at INTEGER
);
```

## Phần mềm

Python 3 trong venv. Thư viện dự kiến: `pyserial`, `adafruit-circuitpython-dht`, `adafruit-circuitpython-bh1750`, `lgpio` hoặc `gpiozero`, `pandas`, `statsmodels`, `scikit-learn`, Keras (trên PC), TFLite/LiteRT (trên Pi), `requests` hoặc `python-telegram-bot`, `google-genai`.

Dịch vụ dự kiến (systemd): `collector` chạy liên tục, `controller` lặp 5 giây (dự đoán nguồn bụi mỗi vòng), `bot` long polling, `uplink` (chỉ bật khi `cloud.enabled`, có `cloud.url` và `INGEST_TOKEN`), `forecast.timer` mỗi giờ, `morning.timer` 06:30. Bí mật để trong `.env`, không commit: `TELEGRAM_TOKEN`, `GEMINI_API_KEY`, `ALLOWED_CHAT_IDS`, `MQTT_PASSWORD`, `INGEST_TOKEN` (mẫu ở `.env.example`). Bí mật của web nằm trong biến môi trường Render: `DATABASE_URL`, `INGEST_TOKEN`, `WEB_PASSWORD_HASH`, `SESSION_SECRET`.

Cấu hình chung nằm trong `config.yaml`, đọc bằng `aiot.config.load_config()`; mọi script, kể cả `pi_*_client.py` và `server_app.py`, lấy IP broker từ đây và mật khẩu MQTT từ `.env`. Test trên laptop: tạo `.venv` trong repo, cài `requirements-dev.txt`, chạy `.venv/Scripts/python -m pytest`.

## Kiểm thử mục tiêu

| Nhóm | Kiểm thử | Tiêu chí đề xuất |
| ---- | -------- | ---------------- |
| Cảm biến | So với trạm công khai ≥ 7 ngày | Tương quan cao sau hiệu chỉnh tuyến tính |
| Ổn định | Chạy liên tục ≥ 7 ngày | Dữ liệu hợp lệ ≥ 95%, không treo |
| Dự báo | Tập test chia theo thời gian | LSTM vượt persistence ở horizon ≥ 2 giờ |
| Điều khiển | Hộp khói, 10 lần vượt ngưỡng | Phản ứng đúng thời gian cấu hình, không nhảy trạng thái |
| Hiệu quả lọc | Đường PM2.5 khi quạt bật so với tắt | Có khác biệt đo được |
| Telegram | Báo cáo sáng 7 ngày | Đúng giờ ≥ 6/7, gửi bù sau khi mất mạng |
| Gemini | Giả lập lỗi API | Dùng mẫu cố định, bản tin vẫn gửi |
| An toàn | Rút dây PMS7003 | Cảnh báo lỗi, không bật/tắt bừa |

Thử khói chỉ dùng lượng nhỏ, nơi thoáng, có người trông. Quạt mini là mô phỏng, phải ghi rõ trong báo cáo đây là proof of concept. Độ ẩm cao ở Hà Nội có thể làm PMS7003 đọc cao hơn thực tế, nên ghi độ ẩm cùng lúc và đối chiếu trạm công khai.

## Cần chốt

Các mục có ghi "tạm" đã có giá trị mặc định trong `config.yaml`/code, nhóm vẫn cần xác nhận:

- Thang AQI (US EPA hay VN_AQI) và ngưỡng điều khiển cuối cùng — tạm: US EPA 2024, ngưỡng như bảng trên (`control` trong `config.yaml`)
- Vị trí đặt cảm biến, và một hay hai cảm biến PM
- SQLite thay CSV hay dùng song song, và có giữ MQTT + dashboard + camera trong sản phẩm cuối không — tạm: SQLite và CSV song song, collector vẫn gửi MQTT
- Model Gemini và số lần gọi mỗi ngày — tạm: `gemini-3.5-flash-lite`, tối đa 3 lần/ngày
- Mốc nộp bài và phân công

## Làm việc với Pi

- SSH bằng key: `ssh admin@10.157.117.41` (hoặc `pi.local`), khi laptop cùng Wi-Fi `abc` với Pi. IP có thể đổi theo mạng.
- Code trên Pi ở `~/BTL_IOT`, venv ở `~/BTL_IOT/venv`. User là `admin`, không có `/home/pi`.
- Bật một lệnh: trên laptop `powershell -ExecutionPolicy Bypass -File scripts\up.ps1` (Mosquitto, chép bản HEAD + `.env` lên Pi, đặt `mqtt.host` trên Pi, chạy `pi_up.sh`, mở dashboard); trên Pi `bash scripts/pi_up.sh [--no-fan|--with-yolo|--stop|--status]` (idempotent: cài phần thiếu, cập nhật unit, chỉ khởi động lại dịch vụ khi code/cấu hình/.env/thư viện đổi). YOLO (torch, ultralytics) tách ra `requirements-pi-camera.txt`, chỉ cài với `--with-yolo`.
- Bảng kiểm tra: `venv/bin/python -m aiot.healthcheck` (không cần sudo); mã thoát 1 chỉ khi collector không chạy hoặc không có mẫu mới < 60 s. Các bước thủ công chuyển sang `docs/van_hanh_chi_tiet.md`.
- Collector (`python -m aiot.collector`, dịch vụ `aiot-collector`) thay cho `pi_sensor_client.py`; không chạy cả hai cùng lúc vì dùng chung cảm biến.
- `test_all.py` đã có trong repo. `test.py` trên Pi đã cũ, vẫn yêu cầu ADS1115. Thay đổi chưa commit của `pi_sensor_client.py` trên Pi (đường dẫn CSV) đã đưa vào repo; Pi cần cập nhật code từ repo và tạo `.env` có `MQTT_PASSWORD`.
- Chạy kiểm tra: `ssh -t admin@10.157.117.41 "cd ~/BTL_IOT && source venv/bin/activate && python test_all.py"`. Phần quạt và còi cần người đứng cạnh mạch trả lời; không có TTY thì script bỏ qua, không đụng relay và còi, và mã thoát là 1. Phải dừng dịch vụ trước (`bash scripts/pi_up.sh --stop`), nếu không script tự thoát và nhắc.
- Tắt Pi và rút cáp nguồn trước khi đổi dây. Không nối hay tháo dây khi Pi đang có điện.
- Khi đổi cách nối, cập nhật `so_do_noi_day_raspberry_pi.md` và file này.
- Không ghi mật khẩu, token hay API key vào file này hoặc tài liệu.
