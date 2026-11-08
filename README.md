# Trạm quan trắc không khí AIoT

Bài tập nhóm môn IoT, dựa trên Chương 12 *Hands-On Artificial Intelligence for IoT*.
Raspberry Pi 4 đo không khí, lưu cục bộ, dự báo PM2.5, tự bật/tắt quạt lọc, báo qua Telegram
(kèm lời khuyên Gemini và ảnh camera). Laptop dùng để xem dashboard và huấn luyện LSTM.

- Các bước thủ công, demo quạt, huấn luyện LSTM, nghiệm thu, xử lý sự cố:
  [docs/van_hanh_chi_tiet.md](docs/van_hanh_chi_tiet.md)
- Cách nối dây: [so_do_noi_day_raspberry_pi.md](so_do_noi_day_raspberry_pi.md) ·
  quy tắc dự án: [CLAUDE.md](CLAUDE.md) · nghiệm thu: [docs/nghiem_thu.md](docs/nghiem_thu.md)

---

## Chạy nhanh

Cần: Pi đã cài Raspberry Pi OS với user `admin`, cùng Wi-Fi với laptop, và laptop SSH được vào
Pi bằng key (`ssh admin@10.157.117.41` vào thẳng, không hỏi mật khẩu). Mạch đã nối theo
`so_do_noi_day_raspberry_pi.md`.

### 1. Chuẩn bị `.env` (một lần)

Chép `.env.example` thành `.env` ở thư mục repo trên laptop rồi điền. File này không commit;
script tự chép lên Pi và đặt quyền `600`.

| Khoá | Lấy ở đâu | Thiếu thì |
| ---- | --------- | --------- |
| `TELEGRAM_TOKEN` | Telegram: nhắn `@BotFather` → `/newbot` → token dạng `123456789:AA...` | bot không chạy |
| `ALLOWED_CHAT_IDS` | mã chat được phép dùng bot, cách nhau dấu phẩy (xem dưới) | bot không chạy |
| `GEMINI_API_KEY` | Google AI Studio → API key | báo cáo dùng lời khuyên mẫu |
| `MQTT_PASSWORD` | mật khẩu user `pi01` của Mosquitto trên laptop | collector bỏ qua MQTT, dashboard trống |

**Lấy chat id:** gửi `/start` cho bot (cả nhóm dùng thì thêm bot vào nhóm và nhắn một tin), rồi
mở trên máy của bạn `https://api.telegram.org/bot<TOKEN>/getUpdates` và đọc `message.chat.id`
(chat nhóm có id âm). Không dán link có token vào đâu cả. Lộ token thì `/revoke` trong BotFather.

### 2. Cách 1 — từ laptop (khuyến nghị)

Mở PowerShell ở thư mục repo:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\up.ps1
```

Script làm lần lượt: bật Mosquitto (`%USERPROFILE%\mosq\mosquitto.conf`, cửa sổ thu nhỏ) nếu
chưa chạy → tìm Pi (`10.157.117.41`, rồi `pi.local`) → đóng gói bản **đã commit** (`git archive
HEAD`) và chép cùng `.env` lên `~/BTL_IOT` → đặt `mqtt.host` trên Pi thành IP laptop → chạy
`pi_up.sh` trên Pi (hỏi mật khẩu sudo của `admin` một lần) → mở dashboard
<http://localhost:8501>. Thay đổi chưa commit **không** được đưa lên Pi.

| Tham số | Việc |
| ------- | ---- |
| `-PiHost <IP>` | dùng IP/tên Pi này thay vì tự dò |
| `-NoDeploy` | không chép code, chỉ chạy `pi_up.sh` với code đang có trên Pi |
| `-NoFan` | không chạy bộ điều khiển quạt (`pi_up.sh --no-fan`) |
| `-Status` | chỉ in bảng kiểm tra của Pi |
| `-Stop` | dừng dashboard và Mosquitto trên laptop; thêm `-StopPi` để dừng cả dịch vụ trên Pi |

### 3. Cách 2 — ngay trên Pi

Khi code đã có ở `~/BTL_IOT` (ví dụ `git pull`) và `.env` đã chép lên:

```bash
bash ~/BTL_IOT/scripts/pi_up.sh
```

Lần đầu script cài gói hệ thống, bật I2C/UART, đặt múi giờ `Asia/Ho_Chi_Minh`, thêm `admin`
vào nhóm `gpio,i2c,dialout,video`, tạo venv và cài `requirements-pi.txt` (có thể mất 10–30
phút). Các lần sau chỉ cập nhật unit systemd đã đổi và khởi động lại dịch vụ để nạp code mới.
Nếu script báo đã đổi I2C/UART thì chạy `sudo reboot` một lần.

| Cờ | Việc |
| -- | ---- |
| (không cờ) | cài phần còn thiếu, bật `aiot-collector`, `aiot-controller`, `aiot-bot` và 3 timer, in bảng kiểm tra |
| `--no-fan` | như trên nhưng dừng và tắt tự khởi động `aiot-controller` (chưa làm demo quạt) |
| `--stop` | dừng mọi dịch vụ và timer; controller dừng thì quạt tắt (vẫn tự chạy khi bật lại Pi) |
| `--status` | chỉ in bảng kiểm tra, không cần sudo |
| `--help` | hướng dẫn |

Lần đầu bật quạt tự động nên làm demo với DB thử trước
([chi tiết 5.3](docs/van_hanh_chi_tiet.md#53-điều-khiển-quạt-aiot-controller)), nên có thể chạy
`--no-fan` trước rồi chạy lại không cờ.

### 4. Lệnh bot Telegram

Gõ `/` trong Telegram để thấy menu. Bot chỉ trả lời chat có trong `ALLOWED_CHAT_IDS`.

| Lệnh | Việc |
| ---- | ---- |
| `/now` | số liệu cảm biến mới nhất (PM, nhiệt độ, độ ẩm, ánh sáng, MQ135) |
| `/status` | chế độ, quạt, ghi đè, lỗi cảm biến, tuổi dữ liệu, giờ yên lặng |
| `/forecast` | dự báo PM2.5 6 giờ tới kèm nhãn AQI |
| `/report` | báo cáo 24 giờ qua (tính vào 3 lượt Gemini/ngày) |
| `/photo` | chụp ảnh ban công ngay |
| `/fan on` / `/fan off` | bật/tắt quạt thủ công trong 2 giờ |
| `/mode auto` / `/mode manual` | quạt theo PM2.5 / giữ quạt như hiện tại trong 2 giờ |
| `/quiet 22 6` | giờ yên lặng 22h → 6h (chỉ cảnh báo khẩn cấp được gửi) |
| `/clear` | xoá tin của bot và của bạn trong 48 giờ qua |
| `/help` | danh sách lệnh |

Bot tự gửi: cảnh báo đổi nhóm AQI, mức khẩn cấp, lỗi cảm biến, báo cáo 06:30, cảnh báo nắng /
sắp mưa kèm ảnh. Lệnh điều khiển cũ hơn 2 phút bị bỏ để quạt không bật bất ngờ.

### 5. Đọc bảng kiểm tra

Cuối `pi_up.sh` (và `--status`, `up.ps1 -Status`) in bảng do `python -m aiot.healthcheck` tạo:

| Ký hiệu | Nghĩa |
| ------- | ----- |
| ✅ | đạt |
| ❌ | mục **quan trọng** chưa đạt: `aiot-collector` không chạy hoặc không có mẫu mới trong 60 giây; mã thoát 1 |
| ⚠️ | cảnh báo, trạm vẫn đo được (ví dụ timer chưa bật, thiếu khoá `.env`, camera không chụp được) |
| ➖ | bỏ qua có chủ ý (`--no-fan`, thiếu token Telegram, client camera cũ đang giữ camera) |

Các dòng: `.env` (chỉ in tên khoá thiếu, không in giá trị), trạng thái 6 unit systemd, tuổi
mẫu mới nhất, cảm biến không có số (MQ135 trống trong 3 phút sấy đầu là bình thường), nhịp của
controller, Telegram `getMe`, chụp thử camera, đồng hồ đã đồng bộ NTP chưa.
Có ❌ thì xem `journalctl -u aiot-collector -n 30` và
[bảng xử lý sự cố](docs/van_hanh_chi_tiet.md#10-xử-lý-sự-cố).

---

## Tổng quan

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

| Dịch vụ (systemd) | Chạy | Việc |
| ----------------- | ---- | ---- |
| `aiot-collector` | liên tục | đọc cảm biến mỗi 10 s → SQLite + CSV + MQTT, gộp trung bình 5 phút |
| `aiot-controller` | liên tục | luật quạt mỗi 20 s, lệnh `/fan`, `/mode` |
| `aiot-bot` | liên tục | bot Telegram, gửi cảnh báo và báo cáo |
| `aiot-forecast.timer` | mỗi giờ, phút 02 | dự báo PM2.5 6 giờ tới |
| `aiot-morning.timer` | 06:30 hằng ngày | báo cáo sáng + lời khuyên Gemini |
| `aiot-weather.timer` | mỗi 5 phút | luật nắng/mưa, chụp ảnh khi cảnh báo |

Test trên laptop: `.venv\Scripts\python -m pytest`. **Tắt Pi và rút cáp nguồn trước khi đổi dây.**
