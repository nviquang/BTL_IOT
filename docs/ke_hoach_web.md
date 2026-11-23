# Kế hoạch: web công khai xem dự đoán và điều khiển quạt, còi

## Mục tiêu

Một trang web mở được từ bất kỳ đâu, có 4 việc:

1. Xem số liệu hiện tại, nhãn nguồn bụi AI cùng xác suất từng nhãn, và dự báo PM2.5 6 giờ tới.
2. Chuyển giữa chế độ **tự động** (AI và luật quyết định) và **thủ công**.
3. Ở chế độ thủ công: bật/tắt quạt và bật/tắt còi.
4. Chạy ổn định khi chỉ có Pi và Internet, không cần laptop.

## Đã chốt với nhóm

- **Nơi chạy:** web chạy ngay trên Pi, đọc thẳng file SQLite. **Cloudflare Tunnel** đưa web ra Internet bằng HTTPS, không phải mở port trên router.
- **Đăng nhập:** không có mật khẩu, ai có link đều xem và điều khiển được (theo yêu cầu).
  - Để giảm rủi ro, plan vẫn giữ: lệnh thủ công tự hết hạn, giới hạn số lệnh mỗi phút, ghi nhật ký IP người bấm, và luôn có nút về tự động.
- **URL:** trước mắt dùng Quick Tunnel miễn phí (`*.trycloudflare.com`, không cần tài khoản).
  - URL đổi mỗi lần tunnel khởi động lại. Bot Telegram sẽ tự gửi URL mới, và lệnh `/web` trả về URL hiện tại.
  - Nếu sau này có tên miền trên Cloudflare thì đổi sang Named Tunnel để có URL cố định, chỉ sửa cấu hình, không sửa code.

## Kiến trúc

```
Trình duyệt ──HTTPS──► Cloudflare ──tunnel (Pi kết nối ra)──► cloudflared (Pi)
                                                               │
                                                               ▼ http://127.0.0.1:8080
                                    aiot-web (FastAPI) ── đọc ─► SQLite ◄── collector / controller
                                         │ ghi lệnh (khoá state)        ▲
                                         └──────────────────────────────┘ controller áp dụng
```

- **Web không chạm GPIO.** Lệnh từ web đi qua đúng đường lệnh Telegram đang đi: ghi khoá state, controller đọc và áp dụng trong ≤ 5 giây, kèm kiểm tra tuổi lệnh và thời hạn ghi đè. Như vậy giữ nguyên nguyên tắc chỉ controller ghi relay và còi.
- **Mất Internet:** web công khai không vào được. Trạm vẫn đo, vẫn điều khiển, và web vẫn mở được trong mạng nhà qua `http://<IP Pi>:8080` nếu bật `web.lan: true`.

## Các bước

### 1. Gom lệnh điều khiển vào một module dùng chung — `aiot/commands.py`

- Tách từ `aiot/bot.py` các hàm `_pending_json`, `_apply_control`, `cmd_fan`, `cmd_mode`, `cmd_mute` thành các hàm thuần:
  - `set_mode(conn, cfg, now, mode, source)`
  - `set_fan(conn, cfg, now, on, source)`
  - `set_buzzer(conn, cfg, now, on, source)`
- Mỗi lệnh ghi sự kiện `command` với `source="telegram chat=…"` hoặc `"web ip=…"`.
- Bot gọi lại các hàm này. Hành vi của bot giữ nguyên, các test bot hiện có phải vẫn đạt.

### 2. Còi thủ công (hiện chỉ có `/mute`)

- Thêm khoá `pending_buzzer` dạng `{"on": true|false|null, "ttl_s": ..., "at": ...}`. Controller áp dụng giống `pending_override` và lưu vào khoá `buzzer_override`.
- `aiot/alarm.py`:
  - Còi đang bị ghi đè **bật**: kêu từng hồi `burst_s` liên tục. Ghi đè bật còi tối đa `buzzer.manual_max_s` (mặc định 60 s), hết hạn thì tự tắt, để tránh ai đó bật còi kêu mãi.
  - Còi đang bị ghi đè **tắt**: im, giống `/mute`, trong `control.override_ttl_s`.
  - Ghi đè là `null`: theo nhãn AI hoặc luật ngưỡng như hiện tại.
- **Chế độ thủ công** là khi quạt hoặc còi đang có ghi đè. **Về tự động** xoá cả hai ghi đè.
- Bot thêm lệnh `/buzzer on|off`, và `/mode auto` xoá cả ghi đè còi.

### 3. Dịch vụ web — `aiot/web.py`, `aiot/web_static/index.html`

- FastAPI + uvicorn. Chỉ nghe trên `127.0.0.1:8080`; cloudflared nối vào đó.
- Thêm `fastapi` và `uvicorn` vào `requirements-pi.txt`.
- **API đọc** (JSON):

  | Đường dẫn | Trả về |
  | --------- | ------ |
  | `GET /api/status` | Mẫu mới nhất; nhãn AI và xác suất; trạng thái quạt và còi, chế độ, hạn ghi đè; tuổi nhịp controller; cảnh báo khi dữ liệu cũ |
  | `GET /api/history?hours=6` | PM1/PM2.5/PM10, nhiệt độ, độ ẩm từ `raw_readings`, kèm nhãn từ `source_predictions` để tô màu nền biểu đồ |
  | `GET /api/predictions?minutes=60` | Nhãn và xác suất 5 giây/lần |
  | `GET /api/forecast` | Dự báo 6 giờ, model, thời điểm tạo |
  | `GET /api/events?limit=30` | Nhật ký quạt, còi, đổi nguồn bụi, lệnh |

- **API ghi:** `POST /api/mode {auto|manual}`, `POST /api/fan {on|off}`, `POST /api/buzzer {on|off}`.
  - Các lệnh gọi `aiot/commands.py`.
  - Mỗi IP tối đa 10 lệnh mỗi phút, vượt thì trả 429.
  - Lấy IP người bấm từ header `CF-Connecting-IP`.
- **Giao diện:** một trang HTML + JavaScript thuần, gọi API 5 giây/lần, biểu đồ bằng Chart.js.
  - Ô lớn hiện nguồn bụi hiện tại (màu theo nhãn), thanh xác suất 4 nhãn, và hành động đang làm.
  - Thẻ số liệu PM2.5 kèm nhóm AQI, nhiệt độ, độ ẩm, ánh sáng.
  - Biểu đồ 6 giờ, nền tô theo nhãn AI; dự báo 6 giờ tới; nhật ký.
  - Công tắc Tự động/Thủ công. Nút quạt và còi chỉ bấm được ở chế độ thủ công, và có hiện thời gian còn lại của ghi đè.
  - Dòng ghi chú: đây là mô hình proof of concept, quạt mini mô phỏng máy lọc.
- Unit `deploy/aiot-web.service`, chạy cùng user `admin`, tự khởi động lại khi lỗi.

### 4. Đưa ra Internet bằng Cloudflare Tunnel

- `scripts/pi_up.sh` tự cài `cloudflared` cho arm64: thêm kho apt chính thức của Cloudflare, chỉ cài khi chưa có.
- Unit `deploy/aiot-tunnel.service` chạy `cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080`.
- Script nhỏ `aiot/tunnel_url.py` đọc URL `https://….trycloudflare.com` trong log của cloudflared và ghi vào khoá state `public_url`.
  - Khi URL đổi, xếp một tin Telegram `web_url` vào outbox: "Web trạm: <URL>".
  - Bot thêm lệnh `/web`.
- `config.yaml` thêm mục `web:` gồm `enabled`, `port`, `lan`, `tunnel` (`quick` hoặc `named`), `tunnel_name`, `rate_limit_per_min`, và thêm `buzzer.manual_max_s`.
- `pi_up.sh --status` và `aiot.healthcheck` hiện trạng thái `aiot-web`, `aiot-tunnel` và URL công khai.

### 5. Test, triển khai, tài liệu

- **Test (pytest):**
  - `aiot/commands.py`: các lệnh ghi đúng khoá; bot vẫn đạt toàn bộ test cũ.
  - Ghi đè còi trong `alarm.py`: bật bị cắt sau `manual_max_s`, tắt thì im, `null` theo AI.
  - API qua `fastapi.testclient`: status, history, lệnh trả 200, sai tham số trả 422, vượt giới hạn trả 429.
  - Đọc URL tunnel từ dòng log mẫu.
- **Chạy thử trên laptop:** `uvicorn aiot.web:app` với DB thử, cộng `inject_pm.py --scenario …` để xem nhãn đổi trên trang, rồi bấm thử các nút.
- **Triển khai:** `scripts/up.ps1`. Bước này cần mật khẩu sudo để cài `cloudflared` và các unit mới.
  - Kiểm tra mở URL từ 4G trên điện thoại.
  - Kiểm tra bấm quạt và còi trên web thì mạch phản hồi trong ≤ 5 giây (cần người đứng cạnh mạch).
- **Tài liệu:**
  - README: mục "Web công khai".
  - CLAUDE.md: bảng trạng thái, lệnh `/buzzer` và `/web`, dịch vụ `aiot-web` và `aiot-tunnel`.
  - Ghi rõ rủi ro không có mật khẩu.

## Rủi ro và việc cần nhóm biết

- **Không có mật khẩu:** ai có link đều điều khiển được. Quick Tunnel cho URL ngẫu nhiên, khó đoán, nhưng **không phải là bảo mật**. Muốn khoá lại sau này thì chỉ cần đặt `WEB_PASSWORD` trong `.env`; code để sẵn chỗ cho việc này nhưng tắt mặc định.
- **Quick Tunnel:** Cloudflare mô tả là dùng để thử nghiệm, không có cam kết hoạt động liên tục, và URL đổi mỗi lần khởi động lại. Để demo hay nộp bài ổn định nên có tên miền và dùng Named Tunnel (cần tài khoản Cloudflare miễn phí).
- **Cài `cloudflared`:** phần mềm lấy từ kho chính thức của Cloudflare, cài trên Pi bằng sudo.
- **Tải trên Pi:** web gọi API 5 giây/lần đọc SQLite. Nhẹ nếu ít người xem; trang có thêm bộ đệm 2 giây cho `/api/status`.
