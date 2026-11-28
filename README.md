# Trạm quan trắc không khí AIoT

Bài tập nhóm môn IoT, dựa trên Chương 12 *Hands-On Artificial Intelligence for IoT*.
Raspberry Pi 4 đo không khí, lưu cục bộ, dự báo PM2.5, tự bật/tắt quạt lọc, báo qua Telegram
(kèm lời khuyên Gemini và ảnh camera). Pi gửi dữ liệu trực tiếp lên Render qua HTTPS.
Mở [web của trạm](https://aiot-tram.onrender.com/) để xem số liệu và điều khiển.

- Các bước thủ công, demo quạt, huấn luyện bộ nhận diện nguồn bụi, nghiệm thu, xử lý sự cố:
  [docs/van_hanh_chi_tiet.md](docs/van_hanh_chi_tiet.md)
- Cách nối dây: [so_do_noi_day_raspberry_pi.md](so_do_noi_day_raspberry_pi.md) ·
  quy tắc dự án: [CLAUDE.md](CLAUDE.md) · nghiệm thu: [docs/nghiem_thu.md](docs/nghiem_thu.md)

---

## Chạy nhanh

Cần Raspberry Pi OS với user `admin` và mạch đã nối theo `so_do_noi_day_raspberry_pi.md`.
Pi cần Internet để dùng web Render và Telegram.

### 1. Chuẩn bị `.env` (một lần)

Chép `.env.example` thành `.env` trong thư mục repo trên Pi rồi điền.
Giữ file ngoài Git và đặt quyền `600` (`pi_up.sh` tự đặt).

| Khoá               | Lấy ở đâu                                                              | Thiếu thì                              |
| ------------------ | ---------------------------------------------------------------------- | -------------------------------------- |
| `TELEGRAM_TOKEN`   | Telegram: nhắn `@BotFather` → `/newbot` → token dạng `123456789:AA...` | bot không chạy                         |
| `ALLOWED_CHAT_IDS` | mã chat được phép dùng bot, cách nhau dấu phẩy (xem dưới)              | bot không chạy                         |
| `GEMINI_API_KEY`   | Google AI Studio → API key                                             | báo cáo dùng lời khuyên mẫu            |
| `INGEST_TOKEN`     | cùng giá trị với biến môi trường `INGEST_TOKEN` trên Render           | Pi không đồng bộ với web              |

**Lấy chat id:** gửi `/start` cho bot (cả nhóm dùng thì thêm bot vào nhóm và nhắn một tin), rồi
mở trên máy của bạn `https://api.telegram.org/bot<TOKEN>/getUpdates` và đọc `message.chat.id`
(chat nhóm có id âm). Không dán link có token vào đâu cả. Lộ token thì `/revoke` trong BotFather.

### 2. Khởi động trên Pi

Khi code đã có ở `~/BTL_IOT` (ví dụ `git pull`) và `.env` đã chép lên:

```bash
bash ~/BTL_IOT/scripts/pi_up.sh
```

Lần đầu script cài gói hệ thống, bật I2C/UART, đặt múi giờ `Asia/Ho_Chi_Minh`, thêm `admin`
vào nhóm `gpio,i2c,dialout,video`, tạo venv và cài `requirements-pi.txt` (vài phút). Các lần
sau chỉ cập nhật unit systemd đã đổi; dịch vụ đang chạy chỉ được khởi động lại khi code,
`config.yaml`, `.env`, unit hoặc thư viện đổi (khởi động lại controller lúc quạt đang bật làm
relay nhả rồi đóng lại). Nếu script báo đã đổi I2C/UART thì chạy `sudo reboot` một lần.
Repo phải ở `/home/admin/BTL_IOT` (các unit systemd trỏ vào đó).

| Cờ            | Việc                                                                                                                 |
| ------------- | -------------------------------------------------------------------------------------------------------------------- |
| (không cờ) | cài phần còn thiếu, bật collector, controller và 3 timer. Bật bot và uplink khi có đủ cấu hình |
| `--no-fan`    | như trên nhưng dừng và tắt tự khởi động `aiot-controller` (chưa làm demo quạt)                                       |
| `--force`     | cho chạy khi repo không ở `/home/admin/BTL_IOT`                                                                      |
| `--stop`      | dừng mọi dịch vụ và timer, tắt tự khởi động `aiot-controller` (quạt tắt); dịch vụ khác vẫn tự chạy khi bật lại Pi    |
| `--status`    | chỉ in bảng kiểm tra, không cần sudo                                                                                 |
| `--help`      | hướng dẫn                                                                                                            |

Lần đầu bật quạt tự động nên làm demo với DB thử trước
([chi tiết 5.3](docs/van_hanh_chi_tiet.md#53-điều-khiển-quạt-aiot-controller)), nên có thể chạy
`--no-fan` trước rồi chạy lại không cờ.

### 3. Triển khai qua SSH từ Windows (tuỳ chọn)

Nếu muốn chép bản đã commit và chạy script trên Pi từ PowerShell, cần Git, OpenSSH và SSH key của Pi.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\up.ps1 -PiHost <IP_PI>
```

Script chép code từ `git archive HEAD`, file `.env` và các mô hình hiện có tới `~/BTL_IOT`,
rồi chạy `pi_up.sh` trên Pi. Các cờ `-NoDeploy`, `-NoFan`, `-Status` và `-StopPi` lần lượt
dùng bản đã có trên Pi, bỏ qua controller, xem tình trạng hoặc dừng các dịch vụ Pi.

### 4. Lệnh bot Telegram

Gõ `/` trong Telegram để thấy menu. Bot chỉ trả lời chat có trong `ALLOWED_CHAT_IDS`.

| Lệnh                          | Việc                                                             |
| ----------------------------- | ---------------------------------------------------------------- |
| `/now`                        | số liệu cảm biến mới nhất (PM, nhiệt độ, độ ẩm, ánh sáng)        |
| `/status`                     | chế độ, quạt, ghi đè, lỗi cảm biến, tuổi dữ liệu, giờ yên lặng   |
| `/report`                     | báo cáo 24 giờ qua (tính vào 3 lượt Gemini/ngày)                 |
| `/fan on` / `/fan off`        | bật/tắt quạt thủ công trong 2 giờ                                |
| `/mode auto` / `/mode manual` | quạt theo PM2.5 / giữ quạt như hiện tại trong 2 giờ              |
| `/quiet 22 6`                 | giờ yên lặng 22h → 6h (chỉ cảnh báo khẩn cấp được gửi)           |
| `/mute`, `/mute 10`, `/mute off` | tắt tiếng còi tại chỗ 30 phút / 10 phút / bật lại             |
| `/buzzer on` / `/buzzer off`  | còi thủ công: kêu tối đa 1 phút / giữ im 2 giờ (`/mode auto` xoá) |
| `/web`                        | đường dẫn trang web của trạm                                     |
| `/clear`                      | xoá tin của bot và của bạn trong 48 giờ qua                      |
| `/help`                       | danh sách lệnh                                                   |

Bot tự gửi: cảnh báo đổi nhóm AQI, mức khẩn cấp, lỗi cảm biến, báo cáo 06:30, cảnh báo nắng /
sắp mưa kèm ảnh. Lệnh điều khiển cũ hơn 2 phút bị bỏ để quạt không bật bất ngờ.

### 5. Đọc bảng kiểm tra

Cuối `pi_up.sh` (và `--status`, `up.ps1 -Status`) in bảng do `python -m aiot.healthcheck` tạo:

| Ký hiệu | Nghĩa                                                                                                    |
| ------- | -------------------------------------------------------------------------------------------------------- |
| ✅       | đạt                                                                                                      |
| ❌       | mục **quan trọng** chưa đạt: `aiot-collector` không chạy hoặc không có mẫu mới trong 60 giây; mã thoát 1 |
| ⚠️      | cảnh báo, trạm vẫn đo được (ví dụ timer chưa bật, thiếu khoá `.env`, camera không chụp được)             |
| ➖       | bỏ qua có chủ ý (`--no-fan`, thiếu token Telegram hoặc cấu hình web)                     |

Các dòng: `.env` (chỉ in tên khoá thiếu, không in giá trị), trạng thái 7 unit systemd, tuổi
mẫu mới nhất, cảm biến không có số, nhịp của
controller, lần gửi lên web cloud gần nhất, Telegram `getMe`, chụp thử camera, đồng hồ đã đồng bộ NTP chưa.
Có ❌ thì xem `journalctl -u aiot-collector -n 30` và
[bảng xử lý sự cố](docs/van_hanh_chi_tiet.md#10-xử-lý-sự-cố).

---

## Tổng quan

```text
DHT11, BH1750, PMS7003 → aiot-collector → SQLite trên Pi
                              └───────→ sensors.csv trên Pi

SQLite ↔ aiot-controller → GPIO23 (relay quạt), GPIO5 (còi)
       ↔ aiot-forecast   → dự báo PM2.5 cho 6 giờ tới
       ↔ aiot-uplink     ↔ HTTPS ↔ FastAPI trên Render ↔ Neon Postgres
       ↔ aiot-bot        ↔ Telegram
       ← aiot-morning    → Gemini để viết lời khuyên
       ← aiot-weather    → camera USB để đính kèm ảnh cảnh báo
```

- **Local-first:** đo, lưu, dự báo và điều khiển chạy trên Pi. Mất mạng vẫn đo, vẫn điều khiển;
  tin Telegram được gửi bù khi có mạng.
- **An toàn AI:** Gemini chỉ viết lời khuyên. Quạt và còi theo nhãn của bộ nhận diện nguồn bụi
  chạy cục bộ (bảng hành động cố định), thiếu mô hình/dữ liệu thì theo luật ngưỡng trong `aiot/control.py`.

| Dịch vụ (systemd)     | Chạy             | Việc                                                               |
| --------------------- | ---------------- | ------------------------------------------------------------------ |
| `aiot-collector`      | liên tục         | mẫu 1 s → `fast_readings`; mỗi 10 s → SQLite + CSV, gộp 5 phút |
| `aiot-controller`     | liên tục         | mỗi 5 s: AI nguồn bụi → quạt + còi, lệnh `/fan`, `/mode`             |
| `aiot-bot`            | liên tục         | bot Telegram, gửi cảnh báo và báo cáo                              |
| `aiot-uplink` | mỗi 5 giây khi có cấu hình | gửi dữ liệu và nhận lệnh từ Render qua HTTPS |
| `aiot-forecast.timer` | mỗi giờ, phút 02 | dự báo PM2.5 6 giờ tới bằng seasonal naive hoặc persistence                                             |
| `aiot-morning.timer`  | 06:30 hằng ngày  | báo cáo sáng + lời khuyên Gemini                                   |
| `aiot-weather.timer`  | mỗi 20 giây       | luật nắng/mưa, chụp ảnh khi cảnh báo                               |

Test trên laptop: `.venv\Scripts\python -m pytest`. **Tắt Pi và rút cáp nguồn trước khi đổi dây.**

## Nhận diện nguồn bụi (AI)

Mỗi 5 giây controller dự đoán nguồn bụi từ mẫu 1 giây của 30 phút qua (RandomForest,
`models/source_rf.json`) và điều khiển theo bảng cố định:

| Nhãn                  | Quạt | Còi |
| --------------------- | ---- | --- |
| bình thường           | tắt  | im  |
| nấu ăn                | bật  | im  |
| ô nhiễm từ ngoài vào  | bật  | kêu |
| khói thuốc lá / nhang | tắt  | kêu |

Ghi đè `/fan`, `/mute` vẫn thắng. Không có mô hình, mẫu 1 giây cũ hơn 15 s hoặc chưa đủ tin
cậy thì quay về luật ngưỡng PM2.5. Chi tiết, kết quả và cách thu thêm dữ liệu thật:
`docs/nhan_dien_nguon_bui.md`.

```powershell
.venv\Scripts\python -m training.export_fast --host admin@172.29.23.41   # kéo dữ liệu Pi
.venv\Scripts\python -m training.train_source                            # huấn luyện + đánh giá
```

Gán nhãn sự kiện thật trên Pi: `venv/bin/python scripts/label_event.py --label cooking --start 18:05 --end 18:25 --note "rán trứng"`.
Demo trên DB thử: `venv/bin/python scripts/inject_pm.py --db ~/iot_data/demo.db --scenario smoking`
(chạy kèm `venv/bin/python -m aiot.controller --config <file cấu hình có db_path trỏ tới demo.db>`).

## Web trên cloud

Trang web công khai hiển thị số liệu, nhãn nguồn bụi AI kèm xác suất, biểu đồ 6 giờ vừa qua và nhật ký.
Đăng nhập bằng mật khẩu nhóm để chuyển Tự động/Thủ công và bật/tắt quạt, còi.
Kế hoạch và lý do chọn dịch vụ nằm trong `docs/ke_hoach_web.md`.

```
Pi: aiot-uplink ──HTTPS, 5 s/lần──► Render: cloud/app.py (FastAPI) ◄── trình duyệt
     ▲  ghi lệnh vào khoá state          │
     └── controller áp dụng ≤ 5 s        ▼ Neon Postgres
```

- Pi chỉ kết nối ra ngoài, không mở port. Web không chạm GPIO: lệnh web đi qua đúng đường
  của lệnh Telegram (`aiot/commands.py` → `pending_override`/`pending_buzzer` → controller).
- Lệnh web quá 60 giây chưa được Pi nhận thì hết hạn, không áp dụng muộn. Mỗi lệnh chỉ áp
  dụng một lần. Còi bật thủ công tối đa `buzzer.manual_max_s` (60 s).
- Cloud nhận mẫu mới nhất, lịch sử trung bình 1 phút, trạng thái, dự báo và nhật ký đã lọc thông tin riêng tư. Ảnh được gửi qua Telegram.
- Mất Internet: trạm vẫn đo và điều khiển như cũ; web hiện "Pi mất kết nối" và số liệu cuối.

### Chạy thử trên laptop (không cần Pi, Render, Neon)

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python scripts\cloud_demo.py
```

Mở http://127.0.0.1:8000, mật khẩu thử `demo-local-123`. Script tạo DB giả lập 6 giờ và chạy
uplink + controller với quạt/còi giả (in ra màn hình), nên bấm nút sẽ thấy phản hồi sau vài giây.

### Deploy (một lần, người trong nhóm làm)

1. **Neon** (neon.tech, gói free): tạo project, region gần nhất (Singapore), chép chuỗi kết nối
   `postgresql://...?sslmode=require`.
2. **Tạo bí mật** trên laptop:
   ```powershell
   .venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   ```powershell
   .venv\Scripts\python -m cloud.security
   ```
   Lệnh đầu cho `INGEST_TOKEN`, lệnh sau hỏi mật khẩu web và in `WEB_PASSWORD_HASH`.
3. **Render** (render.com, gói free): New → Blueprint → chọn repo GitHub này (đọc `render.yaml`).
   Điền `DATABASE_URL`, `INGEST_TOKEN`, `WEB_PASSWORD_HASH` khi được hỏi; `SESSION_SECRET` tự
   sinh. Đợi build xong, mở `https://<tên>.onrender.com/healthz` thấy `{"ok":true}`.
4. **Pi**: thêm `INGEST_TOKEN=<cùng giá trị>` vào `.env`; trong `config.yaml` đặt
   `cloud.enabled: true` và `cloud.url: https://<tên>.onrender.com`; chạy
   `bash ~/BTL_IOT/scripts/pi_up.sh` trên Pi. Script tự bật `aiot-uplink`,
   bảng kiểm tra có dòng "web cloud".
5. **Kiểm tra**: mở URL bằng 4G trên điện thoại; đăng nhập, bấm Thủ công → Bật quạt, Kêu còi;
   mạch phản hồi trong ≤ 10 giây (cần người đứng cạnh mạch).

Thu hồi: đổi `WEB_PASSWORD_HASH` trên Render (mật khẩu cũ hết tác dụng), đổi `SESSION_SECRET`
(đăng xuất mọi người), đổi `INGEST_TOKEN` cả hai nơi.

Giới hạn gói free (kiểm tra lại trước khi tạo): Render free ngủ sau 15 phút không có request,
đánh thức mất khoảng 1 phút — khi Pi online thì lượt gửi 5 giây/lần giữ nó thức, khi Pi offline
lâu thì người mở trang đầu tiên phải đợi. Postgres free của Render hết hạn sau 30 ngày nên dùng Neon.
