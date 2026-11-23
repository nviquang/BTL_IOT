# Kế hoạch: web trên cloud xem dự đoán AI và điều khiển quạt, còi

Thay cho bản kế hoạch trước (web chạy trên Pi + Cloudflare Tunnel, không mật khẩu).

**Trạng thái 23/11/2026:** bước 1–4 và 6 đã làm xong (code, test, chạy thử cục bộ bằng `scripts/cloud_demo.py`). Bước 5 (deploy) cần nhóm tạo tài khoản Render và Neon theo README mục "Web trên cloud".

## Mục tiêu

Một trang web có URL cố định, ai cũng mở được, làm 4 việc:

1. Xem số liệu hiện tại, nhãn nguồn bụi AI và xác suất 4 nhãn, dự báo PM2.5 6 giờ tới (kèm nhóm AQI).
2. Chuyển chế độ **tự động** ↔ **thủ công**.
3. Ở chế độ thủ công: bật/tắt quạt, bật/tắt còi.
4. Vẫn xem được dữ liệu gần nhất khi Pi mất mạng, kèm thông báo "Pi mất kết nối lúc …".

## Đã chốt

- **Nơi chạy:** web chạy trên cloud, không chạy trên Pi.
- **Quyền:** ai có link cũng **xem** được. **Điều khiển** cần mật khẩu chung của nhóm (`WEB_PASSWORD`).
- **Giữ nguyên nguyên tắc:** chỉ `controller` trên Pi chạm GPIO. Luật điều khiển chạy cục bộ, không phụ thuộc cloud. Cloud chết thì trạm vẫn đo và vẫn điều khiển như bây giờ.

## Chọn dịch vụ (đề xuất, kiểm tra lại giá trước khi tạo tài khoản)

| Thành phần | Đề xuất | Lý do |
| ---------- | ------- | ----- |
| Web + API | **Render**, Free web service, FastAPI (Python) | Cùng ngôn ngữ với nhóm, deploy từ GitHub, có HTTPS và URL `*.onrender.com` cố định |
| Cơ sở dữ liệu | **Neon** Postgres, gói free | Render free mất dữ liệu ổ đĩa khi ngủ; Postgres free của Render hết hạn sau 30 ngày |

Đặc điểm gói free của Render cần biết:
- Service ngủ sau 15 phút không có request vào, đánh thức mất khoảng 1 phút.
- Pi đẩy dữ liệu 5 giây/lần nên service luôn thức khi Pi online. Đây là lưu lượng thật, không phải ping giữ thức.
- Khi Pi offline > 15 phút, người mở trang đầu tiên phải đợi khoảng 1 phút.
- Fly.io không còn gói free cho tài khoản mới, nên không chọn.

## Kiến trúc

```
                    HTTPS (Pi chỉ kết nối ra, không mở port)
Pi: aiot-uplink ─── POST /api/ingest (token) ──►  Render: FastAPI ◄── trình duyệt
     │   ▲                ◄── lệnh đang chờ ───        │              (xem công khai,
     │   │                                             ▼               lệnh cần đăng nhập)
     ▼   │ đọc                                    Neon Postgres
  SQLite (collector, controller)
     ▲
     └─ uplink ghi lệnh vào khoá state → controller áp dụng ≤ 5 s
```

**Đường đi của một lệnh:**

1. Người dùng đã đăng nhập bấm "Bật quạt" → `POST /api/command` → cloud lưu lệnh, trạng thái `pending`.
2. Lần `ingest` tiếp theo, cloud trả lệnh đó về cho Pi.
3. `aiot-uplink` ghi lệnh vào khoá state, dùng đúng hàm mà bot Telegram đang dùng.
4. `controller` áp dụng lệnh trong ≤ 5 giây.
5. Lần `ingest` sau, Pi gửi trạng thái quạt/còi mới kèm xác nhận `done` (hoặc `rejected` kèm lý do).
6. Trang web hiện "đang chờ Pi…" cho tới khi có xác nhận.
7. Lệnh quá 60 giây chưa được Pi nhận thì bị đánh dấu `expired`, không áp dụng muộn.

## Các bước

### 1. Gom lệnh điều khiển thành module dùng chung — `aiot/commands.py`

- Tách từ `aiot/bot.py` các phần `_pending_json`, `_apply_control`, `cmd_fan`, `cmd_mode`, `cmd_mute` thành các hàm thuần:
  - `set_mode(conn, cfg, now, mode, source)`
  - `set_fan(conn, cfg, now, on, source)`
  - `set_buzzer(conn, cfg, now, on, source)`
- Mỗi lệnh ghi sự kiện `command`, với `source` là `"telegram chat=…"` hoặc `"web user=… ip=…"`.
- Bot gọi lại các hàm này. Toàn bộ test bot hiện có phải vẫn đạt.

### 2. Còi thủ công (hiện chỉ có `/mute`)

- Thêm khoá state `pending_buzzer` dạng `{"on": true|false|null, "ttl_s": …, "at": …}`. Controller áp dụng giống `pending_override` và lưu vào khoá `buzzer_override`.
- Sửa `aiot/alarm.py`:
  - **Ghi đè bật:** kêu từng hồi `burst_s` liên tục, tối đa `buzzer.manual_max_s` (mặc định 60 s) rồi tự tắt, để không ai bật còi kêu mãi.
  - **Ghi đè tắt:** im, giống `/mute`.
  - **Không ghi đè:** theo nhãn AI hoặc luật ngưỡng như hiện tại.
- **Chế độ thủ công** nghĩa là quạt hoặc còi đang có ghi đè. **Về tự động** xoá cả hai ghi đè.
- Bot thêm `/buzzer on|off`. `/mode auto` xoá cả ghi đè còi.

### 3. Dịch vụ cloud — thư mục `cloud/` (deploy riêng lên Render)

- **Thư viện:** FastAPI, uvicorn, SQLAlchemy hoặc `psycopg`. Có `cloud/requirements.txt` riêng và `render.yaml` (Blueprint).
- **Bảng Postgres:**

  | Bảng | Nội dung | Giữ |
  | ---- | -------- | --- |
  | `snapshot` | 1 dòng: mẫu mới nhất, nhãn AI và xác suất, trạng thái quạt/còi, chế độ, hạn ghi đè, `last_seen` | luôn |
  | `history` | Trung bình 1 phút PM1/PM2.5/PM10, nhiệt độ, độ ẩm, lux, nhãn AI | 7 ngày |
  | `forecasts` | Lần dự báo mới nhất: 6 horizon, model, `made_at` | lần mới nhất + 7 ngày |
  | `events` | Nhật ký quạt, còi, đổi nguồn bụi, lệnh | 500 dòng gần nhất |
  | `commands` | `id, kind, value, user, ip, created_at, status, done_at, detail` | 30 ngày |

  Cloud chỉ nhận bản tổng hợp, không nhận mẫu 1 giây, để nằm trong giới hạn dung lượng free.

- **API cho Pi** (header `Authorization: Bearer <INGEST_TOKEN>`):
  - `POST /api/ingest`: nhận snapshot, các dòng history/forecast/events mới, và xác nhận lệnh. Trả về danh sách lệnh `pending`.
  - Thiếu token hoặc sai token thì trả 401.

- **API công khai** (chỉ đọc): `GET /api/status`, `GET /api/history?hours=6`, `GET /api/forecast`, `GET /api/events?limit=30`.
  - `status` có thêm `pi_online`: `last_seen` cách đây < 30 giây.

- **Đăng nhập và lệnh:**
  - `POST /api/login {password}`: so với hash của `WEB_PASSWORD`, rồi đặt cookie phiên đã ký (`HttpOnly`, `Secure`, `SameSite=Strict`, hạn 12 giờ). `POST /api/logout` để đăng xuất.
  - `POST /api/command {kind: mode|fan|buzzer, value}`: cần phiên đăng nhập. Mỗi IP tối đa 10 lệnh/phút, sai mật khẩu tối đa 5 lần/phút; vượt thì trả 429.
  - Pi offline thì vẫn nhận lệnh nhưng báo rõ "Pi đang mất kết nối, lệnh sẽ hết hạn sau 60 s".
  - IP người bấm lấy từ `X-Forwarded-For` của Render.

- **Giao diện:** `cloud/static/index.html`, HTML + JavaScript thuần, gọi API 5 giây/lần, biểu đồ Chart.js.
  - Ô lớn: nguồn bụi hiện tại (màu theo nhãn), thanh xác suất 4 nhãn, hành động đang làm.
  - Thẻ số liệu: PM2.5 kèm nhóm AQI, nhiệt độ, độ ẩm, ánh sáng.
  - Biểu đồ 6 giờ, nền tô theo nhãn AI; dự báo 6 giờ tới; nhật ký.
  - Khối điều khiển: nút đăng nhập; công tắc Tự động/Thủ công; nút quạt và còi chỉ bấm được khi đã đăng nhập và đang ở chế độ thủ công; hiện thời gian ghi đè còn lại và trạng thái lệnh (đang chờ / xong / hết hạn).
  - Dải cảnh báo khi Pi offline. Dòng ghi chú: proof of concept, quạt mini mô phỏng máy lọc.
  - Dùng lại `aiot/aqi.py` cho nhãn AQI: chép sang `cloud/`, hoặc cloud import gói `aiot` (chọn khi làm, tránh kéo theo thư viện phần cứng).

### 4. Dịch vụ đẩy dữ liệu trên Pi — `aiot/uplink.py`, dịch vụ `aiot-uplink`

- Lặp 5 giây:
  1. Đọc SQLite: mẫu mới nhất, `source_predictions` mới nhất, trạng thái controller, các dòng history/forecast/events chưa gửi. Mốc đã gửi lưu ở khoá state `uplink_cursor`.
  2. Gửi `POST /api/ingest`, timeout 10 s.
  3. Ghi các lệnh nhận về qua `aiot/commands.py` với `source="web user=… ip=…"`.
  4. Lần gửi sau kèm kết quả của các lệnh đó.
- Kiểm tra lệnh trước khi ghi: chỉ nhận `kind`/`value` hợp lệ, bỏ lệnh có `created_at` quá 60 s. Mỗi `id` chỉ áp dụng một lần (lưu id đã xử lý), để Pi gửi lại sau khi mất mạng không làm lệnh chạy hai lần.
- Mất mạng: chỉ ghi log, lùi thời gian thử dần tối đa 60 s, có mạng lại thì gửi bù history. Không ảnh hưởng `collector` hay `controller`.
- `config.yaml` thêm mục `cloud:` gồm `enabled`, `url`, `interval_s`, `history_step_s`. `.env` thêm `INGEST_TOKEN`; `.env.example` thêm dòng mẫu.
- Unit `deploy/aiot-uplink.service`, cài và cập nhật bằng `scripts/pi_up.sh` giống các dịch vụ khác. `aiot.healthcheck` và `pi_up.sh --status` hiện lần gửi thành công gần nhất.
- Bot thêm lệnh `/web` trả về URL trang.

### 5. Deploy

1. Tạo project Neon, lấy `DATABASE_URL`.
2. Tạo Web Service trên Render từ repo GitHub. Root là `cloud/`. Đặt biến môi trường `DATABASE_URL`, `INGEST_TOKEN`, `WEB_PASSWORD_HASH`, `SESSION_SECRET` trong dashboard Render, không ghi vào repo.
3. Cloud tự tạo bảng khi khởi động. Kiểm tra `GET /api/status` trả về `pi_online: false`.
4. Đặt `cloud.url` và `INGEST_TOKEN` trên Pi, chạy `scripts/up.ps1`. Kiểm tra trang hiện số liệu thật.
5. Mở trang bằng 4G trên điện thoại. Đăng nhập, bấm quạt và còi; mạch phải phản hồi trong ≤ 10 s (5 s uplink + 5 s controller). Cần người đứng cạnh mạch.

Cần người trong nhóm làm (Claude không tạo tài khoản hay nhập mật khẩu): tạo tài khoản Render và Neon, nhập biến môi trường bí mật, nối repo GitHub với Render.

### 6. Test và tài liệu

- **pytest:**
  - `aiot/commands.py`: ghi đúng khoá; bot vẫn đạt toàn bộ test cũ.
  - Ghi đè còi trong `alarm.py`: bật bị cắt sau `manual_max_s`, tắt thì im, không ghi đè thì theo AI.
  - `aiot/uplink.py` với server giả: gửi đúng payload, lệnh trùng id chỉ áp dụng một lần, lệnh cũ bị bỏ, mất mạng không làm crash.
  - Cloud qua `fastapi.testclient` với SQLite tạm thay Postgres: ingest sai token trả 401; lệnh chưa đăng nhập trả 401; sai tham số trả 422; vượt giới hạn trả 429; lệnh quá 60 s chuyển `expired`; vòng đầy đủ command → ingest → xác nhận.
- **Chạy thử trên laptop:** chạy cloud bằng uvicorn, chạy uplink trỏ vào `localhost` với DB thử, dùng `scripts/inject_pm.py --scenario …` để xem nhãn đổi trên trang.
- **Tài liệu:**
  - README: thêm mục "Web trên cloud".
  - CLAUDE.md: bảng trạng thái, lệnh `/buzzer` và `/web`, dịch vụ `aiot-uplink`, biến `.env` mới.
  - Báo cáo: ghi rủi ro và giới hạn của gói free.

## Thứ tự làm và ước lượng

| # | Việc | Phụ thuộc |
| - | ---- | --------- |
| 1 | `aiot/commands.py` + còi thủ công (bước 1–2) | — |
| 2 | Dịch vụ cloud + giao diện (bước 3) | API lệnh từ #1, chỉ cần thống nhất định dạng |
| 3 | `aiot/uplink.py` (bước 4) | #1, định dạng ingest của #2 |
| 4 | Deploy + thử trên mạch (bước 5) | #1–#3 |

Bước 1 và 2 làm song song được nếu chốt trước định dạng JSON của `/api/ingest` và `commands`.

## Rủi ro

- **Gói free thay đổi:** Render và Neon có thể đổi điều khoản. Code không phụ thuộc riêng vào dịch vụ nào (FastAPI + Postgres chuẩn), chuyển sang Railway, Koyeb hay VPS chỉ cần đổi cấu hình.
- **Ngủ khi Pi offline:** người xem đầu tiên đợi khoảng 1 phút. Trang hiện rõ thời điểm Pi gửi dữ liệu lần cuối.
- **Mật khẩu chung:** lộ mật khẩu thì ai cũng điều khiển được. Giảm rủi ro bằng: lệnh tự hết hạn, còi tối đa 60 s, giới hạn số lệnh, nhật ký người/IP, đổi `WEB_PASSWORD_HASH` trên Render là thu hồi ngay.
- **Độ trễ lệnh:** ≤ 10 s, chấp nhận được với quạt và còi. Muốn nhanh hơn thì giảm `interval_s` hoặc dùng long polling, không cần đổi kiến trúc.
- **Riêng tư:** chỉ gửi số liệu môi trường và trạng thái thiết bị lên cloud, không gửi ảnh camera hay chat id Telegram.
