# Trạm quan trắc không khí AIoT — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hoàn thiện chuỗi thu thập cục bộ → dự báo → Gemini → Telegram → tự động bật/tắt quạt lọc trên Raspberry Pi 4.

**Architecture:** Một package Python `aiot/` chạy thành các dịch vụ systemd độc lập, chỉ giao tiếp qua một file SQLite (WAL). `collector` là tiến trình duy nhất đọc cảm biến, `controller` là tiến trình duy nhất giữ GPIO23, `bot` chỉ ghi yêu cầu vào DB. Logic quyết định là hàm thuần, test được trên laptop không cần phần cứng.

**Tech Stack:** Python 3 (laptop 3.11, Pi 3.13, nên không dùng cú pháp chỉ có từ 3.12), SQLite, pyserial, lgpio, adafruit-circuitpython-dht/bh1750, requests, google-genai, pandas/statsmodels/scikit-learn, Keras (laptop/Colab), LiteRT (Pi), pytest.

**Spec:** `CLAUDE.md`, `so_do_noi_day_raspberry_pi.md`

## Hiện trạng đã đối chiếu (05/11/2026)

| Phát hiện | Hệ quả cho plan |
| --------- | --------------- |
| Laptop chỉ có 53 dòng CSV, dòng cuối ngày 23/10; không có dữ liệu PM trong client chính | Thu dữ liệu là việc gấp nhất, vì LSTM và mọi test ≥ 7 ngày đều phải chờ nó |
| `pi_sensor_client.py` trong repo trỏ `/home/pi` và IP broker cũ; bản trên Pi có thay đổi chưa commit | Task 1 phải lấy bản trên Pi về trước |
| Mật khẩu MQTT nằm cứng trong 4 file `.py` đã commit | Task 1 chuyển sang `.env`, đổi mật khẩu broker |
| `normalize()` trong notebook fit trên toàn chuỗi | Không tái dùng hàm này; xem Task 11 |
| Không SSH được vào Pi lúc lập plan | Mọi nhận định về Pi lấy từ `CLAUDE.md`, chưa kiểm chứng lại |

## Bổ sung so với thiết kế trong CLAUDE.md

Cần sửa `CLAUDE.md` cho khớp khi làm xong Task 3 và Task 5:

1. **Thêm bảng `raw_readings`.** `readings` chỉ có trung bình 5 phút, không đủ để tính "TB 10 phút liên tục ≥ 5 phút" hay "mất dữ liệu quá 2 phút".
2. **Thêm bảng `state` và `outbox`.** Dùng để khôi phục sau khởi động lại và gửi bù tin khi có mạng lại.
3. **Thêm cột `forecasts.model`.** Phân biệt dự báo LSTM với dự báo đối chứng khi đánh giá.
4. **Sửa tên mức "Nguy hại ≥ 150".** Tên này trùng với nhóm AQI "Nguy hại ≥ 225,5" nhưng khác ngưỡng. Plan gọi mức 150 là "khẩn cấp" (`danger_pm25`).
5. **Thêm luật chống dao động cho chế độ chủ động.** Nếu đo đã thấp mà dự báo vẫn cao, thiết kế gốc sẽ thoát rồi vào lại liên tục. Plan chặn thoát khi còn dự báo mới > 100.
6. **Mức khẩn cấp dùng trung vị 1 phút, cần ít nhất 3 mẫu.** "Vào ngay" theo từng mẫu thì một mẫu nhiễu cũng bật quạt, còn trung bình thì không loại được nhiễu: một mẫu 900 và năm mẫu 20 cho trung bình 167.

## Giả định tạm cho mục "Cần chốt"

Các giá trị này nằm trong `config.yaml`, đổi được mà không sửa code, trừ khi có ghi chú khác.

| Câu hỏi | Mặc định dùng trong plan |
| -------- | ------------------------ |
| Thang AQI | US EPA 2024, như bảng trong CLAUDE.md |
| Số cảm biến PM | Một PMS7003 |
| SQLite hay CSV | SQLite là nguồn chính; vẫn ghi `sensors.csv` đúng định dạng cũ. Đổi cần sửa code |
| MQTT, dashboard, camera | Giữ nguyên, nhưng không dịch vụ cốt lõi nào phụ thuộc vào chúng |
| MQ135 | Chỉ lưu và hiển thị, không dùng để điều khiển |
| Ghi đè so với mức khẩn cấp | `/fan off` vẫn thắng mức khẩn cấp, nhưng cảnh báo vẫn được gửi |
| Giờ yên lặng | Chỉ cảnh báo khẩn cấp được gửi trong giờ yên lặng |
| PMS7003 đo liên tục (chưa nối SET) | Chấp nhận hao laser trong thời gian làm bài, vì cần dữ liệu liên tục |
| Model Gemini | Kiểm tra lúc làm Task 10 |
| Mốc nộp bài, phân công, vị trí đặt cảm biến | **Chưa có, cần nhóm chốt** |

## Global Constraints

- Local-first: đo, lưu, dự báo và điều khiển không được phụ thuộc Internet, Gemini hay broker MQTT.
- Gemini chỉ viết lời khuyên, không bao giờ là đầu vào của quyết định điều khiển.
- MQ135: `0` = vượt ngưỡng, `1` = chưa vượt. Không gọi là đo CO₂, không suy ra nồng độ.
- Relay kích mức HIGH: GPIO23 HIGH = quạt quay, LOW = quạt dừng.
- `sensors.csv` giữ đúng `timestamp,device_id,temperature_c,humidity_pct,lux`, mỗi 10 giây, không có dòng rỗng.
- Đường dẫn Pi: `/home/admin/iot_data/sensors.csv`, DB `/home/admin/iot_data/aiot.db`, code `~/BTL_IOT`, venv `~/BTL_IOT/venv`.
- Topic MQTT giữ nguyên `nha/pi01/sensor`, `nha/pi01/sensor_status`.
- Bí mật chỉ nằm trong `.env`: `TELEGRAM_TOKEN`, `GEMINI_API_KEY`, `ALLOWED_CHAT_IDS`, thêm `MQTT_PASSWORD`. Không in ra log, không commit.
- Tắt Pi và rút cáp nguồn trước khi đổi dây. Khi đổi dây, cập nhật `so_do_noi_day_raspberry_pi.md` và `CLAUDE.md`.
- Bước cần người đứng cạnh mạch phải chạy với `ssh -t`.
- Chia dữ liệu theo thời gian, scaler chỉ fit trên train. LSTM không hơn đối chứng thì báo cáo trung thực.
- Thử khói chỉ dùng lượng nhỏ, nơi thoáng, có người trông. Báo cáo ghi rõ quạt mini là proof of concept.
- Làm trên nhánh `feat/aiot-core` tách từ `main` sau khi `docs/wiring-diagram` đã merge.

## Review Focus

1. **Pi khởi động lại khi không có mạng:** Pi không có RTC nên đồng hồ sai tới khi NTP đồng bộ. Điều khiển vẫn phải chạy, còn dữ liệu của khoảng đó không được vào tập huấn luyện. Test ở Task 3 và Task 4.
2. **Lệnh Telegram cũ sau khi mất mạng:** `/fan on` gửi từ 5 giờ trước không được thực thi khi có mạng lại. Test ở Task 8.
3. **Token lộ qua exception:** thông báo lỗi của `requests` chứa URL có token, nên log phải che đi. Test ở Task 8.
4. **Số liệu PM cũ trong bộ đệm UART:** DHT11 có thể chặn vòng lặp tới khoảng 7,5 giây, làm khung PMS7003 bị dồn lại. Controller không được ra quyết định theo số cũ. Test ở Task 2.
5. **Một mẫu PM nhiễu đơn lẻ:** một giá trị ≥ 150 không được kích hoạt mức khẩn cấp, còn PM thật sự cao thì phải vào ngay. Test ở Task 5.

---

## Thứ tự thực hiện

| Giai đoạn | Task | Kết quả | Cần Pi? |
| --------- | ---- | ------- | ------- |
| 0. Chuẩn bị | 1 | Khung dự án, cấu hình, bí mật tách khỏi code | Có |
| 1. Dữ liệu (**gấp nhất**) | 2–4 | Pi tự thu đủ 5 thiết bị vào SQLite 24/7 | Có |
| 2. Điều khiển | 5–6 | Quạt tự bật/tắt theo PM2.5, chịu được lỗi | Có, cần người cạnh mạch |
| 3. Telegram | 7–8 | Cảnh báo và lệnh từ xa | Có |
| 4. Dự báo đối chứng, báo cáo | 9–10 | Báo cáo sáng có dự báo và lời khuyên | Có |
| 5. AI (Chương 12) | 11–12 | LSTM được đánh giá trung thực, chạy trên Pi | Chủ yếu laptop |
| 6. Nghiệm thu | 13 | Số liệu cho báo cáo, tài liệu cập nhật | Có |

Giai đoạn 2–4 có thể làm song song với việc Pi đang thu dữ liệu. Task 11 bắt đầu được ngay bằng dữ liệu công khai. Task 12 cần đủ dữ liệu thật, nên hãy tính ngược từ mốc nộp bài để biết còn bao nhiêu ngày thu.

---

### Task 1: Đồng bộ với Pi và dựng khung dự án

**Files:**
- Create: `aiot/__init__.py`, `aiot/config.py`, `config.yaml`, `.env.example`, `requirements-dev.txt`, `pytest.ini` (`pythonpath = .`), `tests/test_config.py`
- Create (lấy từ Pi): `test_all.py`
- Modify: `.gitignore`, `requirements-pi.txt`, `pi_sensor_client.py`, `pi_camera_client.py`, `pi_detection_client.py`, `server_app.py`, `CLAUDE.md` (hiện chưa commit)

**Interfaces:**
- Produces: `load_config(path="config.yaml", env_path=".env") -> Config`. `Config` là dataclass frozen có `db_path`, `csv_path`, `device_id`, `mqtt`, `control: ControlCfg`, `telegram`, `gemini`, `forecast`, `report`, `secrets`.
- Produces: `ControlCfg` với các trường `enter_pm25=55.0`, `enter_mean_s=600`, `enter_hold_s=300`, `exit_pm25=35.0`, `exit_mean_s=900`, `exit_hold_s=900`, `min_state_s=600`, `danger_pm25=150.0`, `danger_window_s=60`, `proactive_pm25=100.0`, `forecast_max_age_s=5400`, `stale_s=120`, `override_ttl_s=7200`, `loop_s=20`, `demo=False`, `demo_wait_s=20`.

- [ ] **Step 1: Khảo sát Pi (laptop và Pi cùng Wi-Fi `abc`)**

Lấy `git diff pi_sensor_client.py` và `test_all.py` về. Áp thay đổi cần giữ vào repo. Đọc `test_all.py` để ghi lại cách nó đọc khung PMS7003 (cho Task 2), sau đó **kiểm tra không có bí mật** rồi mới thêm vào repo.

- [ ] **Step 2: Kiểm tra LiteRT trên Python 3.13 của Pi**

Trong venv tạm: `pip install ai-edge-litert`, rồi import `Interpreter`.
Expected: import được. Nếu không, ghi lại vào đầu Task 12 và chọn ONNX Runtime làm phương án xuất model. Đây là rủi ro lớn nhất của phần AI nên cần biết sớm.

- [ ] **Step 3: Viết test thất bại trong `tests/test_config.py`**

`test_defaults_match_spec` (`enter_pm25 == 55.0`, `stale_s == 120`, `override_ttl_s == 7200`), `test_demo_shortens_waits` (`demo: true` thì `enter_hold_s`, `exit_hold_s`, `min_state_s`, `enter_mean_s`, `exit_mean_s` đều bằng 20, ngưỡng PM giữ nguyên; nếu không rút ngắn cửa sổ trung bình thì demo vẫn phải chờ vài phút), `test_secrets_come_from_env_not_yaml`, `test_repr_hides_secrets` (`"abc123" not in repr(cfg)` khi token là `abc123`), `test_allowed_chat_ids_parsed` (`"1, 2"` thành `{1, 2}`), `test_missing_telegram_token_does_not_raise` (collector phải chạy được khi chưa có token).

- [ ] **Step 4: Chạy test để thấy thất bại**

Run: `pytest tests/test_config.py -v`
Expected: FAIL với `ModuleNotFoundError: aiot`

- [ ] **Step 5: Implement `aiot/config.py`, tạo `config.yaml`, `.env.example`**

Dùng PyYAML; tự đọc `.env`, không thêm thư viện. `.gitignore` thêm `.env`, `*.db`, `*.db-wal`, `*.db-shm`. Thêm `pytest` vào `requirements-dev.txt`; thêm `pyserial`, `lgpio`, `PyYAML`, `requests`, `google-genai` vào `requirements-pi.txt`.

- [ ] **Step 6: Đưa mật khẩu MQTT và IP broker ra khỏi 4 file `.py`**

Đọc từ `MQTT_PASSWORD` và `config.yaml`. Đổi mật khẩu broker vì mật khẩu cũ đã nằm trong lịch sử git. Sửa `CSV_DIR` thành `/home/admin/iot_data`.

- [ ] **Step 7: Chạy test và kiểm tra**

Run: `pytest tests/test_config.py -v` → PASS. `git grep -nE '^PASSWORD *= *"'` → không có kết quả.

- [ ] **Step 8: Commit**

```bash
git add -A aiot tests config.yaml .env.example .gitignore pytest.ini requirements-pi.txt requirements-dev.txt test_all.py CLAUDE.md pi_sensor_client.py pi_camera_client.py pi_detection_client.py server_app.py
git commit -m "chore: scaffold aiot package and move secrets to .env"
```

### Task 2: Đọc PMS7003

**Files:**
- Create: `aiot/pms7003.py`, `tests/test_pms7003.py`

**Interfaces:**
- Produces: `PmsReading(pm1: int, pm25: int, pm10: int)`, `parse_frame(frame: bytes) -> PmsReading` (raise `ValueError`), `PmsReader(port).read_fresh(timeout_s=3.0) -> PmsReading | None`

- [ ] **Step 1: Viết test thất bại**

Dùng helper `make_frame(pm1, pm25, pm10)` dựng khung 32 byte: `0x42 0x4D`, độ dài 28, 13 giá trị uint16 big-endian, checksum là tổng 30 byte đầu.

- `test_parse_reads_atmospheric_fields`: lấy giá trị atmospheric ở byte 10–15, không lấy CF=1 ở byte 4–9. Đối chiếu với cách đọc của `test_all.py`; nếu khác thì nêu ra trước khi làm tiếp.
- `test_bad_checksum_raises`, `test_bad_header_raises`, `test_wrong_length_field_raises`
- `test_read_fresh_resyncs_after_garbage`: serial giả trả `b"\x00\x42" + frame` thì vẫn ra đúng giá trị.
- `test_read_fresh_discards_buffered_frames`: serial giả đang có sẵn khung cũ PM2.5=10, khung đến sau là 80, thì kết quả là 80 và `reset_input_buffer` đã được gọi.
- `test_read_fresh_returns_none_on_timeout`: không có dữ liệu thì trả `None`, không treo. Đây cũng là trường hợp rút dây PMS7003.

- [ ] **Step 2: Chạy test**

Run: `pytest tests/test_pms7003.py -v`
Expected: FAIL (`aiot.pms7003` chưa có)

- [ ] **Step 3: Implement `aiot/pms7003.py`**

Baud 9600. Import `serial` trong `PmsReader.__init__` để laptop chạy test không cần pyserial. Nhận đối tượng serial qua tham số để dễ thay bằng serial giả.

- [ ] **Step 4: Chạy test**

Run: `pytest tests/test_pms7003.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiot/pms7003.py tests/test_pms7003.py
git commit -m "feat: add PMS7003 frame parser and reader"
```

### Task 3: SQLite và gộp 5 phút

**Files:**
- Create: `aiot/db.py`, `aiot/aggregate.py`, `tests/test_db.py`, `tests/test_aggregate.py`
- Modify: `CLAUDE.md` (lược đồ SQLite)

**Interfaces:**
- Produces trong `db.py`: `connect(path) -> sqlite3.Connection` (WAL, `busy_timeout=5000`), `init_schema(conn)`, `insert_raw(conn, RawSample)`, `pm25_series(conn, since_ts) -> list[tuple[int, float]]`, `get_state(conn, key, default=None) -> str | None`, `set_state(conn, key, value)`, `log_event(conn, ts, kind, detail)`, `purge_raw(conn, older_than_ts)`
- Produces: `RawSample(ts, pm1, pm25, pm10, temp, hum, lux, mq135_do, clock_synced)`; các giá trị đo là `None` khi đọc lỗi.
- Produces trong `aggregate.py`: `aggregate_window(samples, start_ts, window_s=300) -> dict`, `aggregate_pending(conn, now)`

Lược đồ bổ sung vào thiết kế trong `CLAUDE.md`:

```sql
CREATE TABLE raw_readings (
  ts INTEGER PRIMARY KEY, pm1 REAL, pm25 REAL, pm10 REAL,
  temp REAL, hum REAL, lux REAL, mq135_do INTEGER,
  clock_synced INTEGER NOT NULL
);
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at INTEGER NOT NULL, kind TEXT NOT NULL,
  text TEXT NOT NULL, sent_at INTEGER
);
-- forecasts: thêm cột model TEXT NOT NULL
```

- [ ] **Step 1: Viết test thất bại**

- `test_wal_and_busy_timeout`, `test_two_connections_write_without_locked_error`: hai tiến trình cùng ghi không bị `database is locked`.
- `test_duplicate_ts_ignored`: chèn trùng `ts` không raise và giữ bản ghi đầu.
- `test_mean_of_window`
- `test_out_of_range_dropped`: loại PM2.5 ngoài 0–1000, nhiệt độ ngoài 0–50, độ ẩm ngoài 0–100, lux ngoài 0–65535.
- `test_mq135_any_zero_gives_zero`: `[1, 1, 0, 1]` cho `0`, vì `0` là vượt ngưỡng; không được lấy trung bình.
- `test_valid_needs_half_of_pm_samples`: 15/30 mẫu PM cho `valid=1`, 14/30 cho `valid=0`.
- `test_dht_failure_keeps_window_valid`: mất nhiệt độ thì `temp` là NULL, `valid` vẫn là 1.
- `test_unsynced_clock_marks_invalid`: có mẫu `clock_synced=0` thì `valid=0`.
- `test_pending_is_idempotent`: chạy `aggregate_pending` hai lần không sinh dòng trùng; cửa sổ chưa kết thúc chưa được gộp; cửa sổ bị bỏ lỡ do crash được gộp bù.
- `test_purge_keeps_recent`

- [ ] **Step 2: Chạy test**

Run: `pytest tests/test_db.py tests/test_aggregate.py -v`
Expected: FAIL (module chưa có)

- [ ] **Step 3: Implement `aiot/db.py` và `aiot/aggregate.py`, cập nhật lược đồ trong `CLAUDE.md`**

- [ ] **Step 4: Chạy test**

Run: `pytest tests/test_db.py tests/test_aggregate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiot/db.py aiot/aggregate.py tests/test_db.py tests/test_aggregate.py CLAUDE.md
git commit -m "feat: add SQLite storage and 5-minute aggregation"
```

### Task 4: Dịch vụ collector và bắt đầu thu dữ liệu thật

**Files:**
- Create: `aiot/collector.py`, `aiot/sensors.py`, `aiot/payload.py`, `deploy/aiot-collector.service`, `tests/test_collector.py`
- Modify: `server_app.py`, `README.md`, `CLAUDE.md`, `so_do_noi_day_raspberry_pi.md` (mục 7)

**Interfaces:**
- Consumes: `PmsReader`, `insert_raw`, `aggregate_pending`, `purge_raw`, `load_config`
- Produces: `collect_once(sensors, conn, csv_writer, publisher, now, clock_synced) -> RawSample`, `is_clock_synced() -> bool` (`timedatectl show -p NTPSynchronized --value`)
- Produces trong `sensors.py`: `read_dht() -> tuple[float, float] | None`, `read_lux() -> float | None`, `read_mq135() -> int | None`; chuyển logic đọc từ `pi_sensor_client.py`, giữ nguyên cách thử lại DHT.

- [ ] **Step 1: Viết test thất bại với cảm biến giả**

- `test_dht_failure_still_stores_pm`: DHT trả `None` thì vẫn lưu PM vào `raw_readings`.
- `test_csv_skipped_when_dht_or_lux_missing`: CSV không có dòng rỗng.
- `test_csv_format_unchanged`: nội dung file đúng từng byte với mẫu `timestamp,device_id,temperature_c,humidity_pct,lux` và dòng `2026-11-05T08:00:00,pi01,28.5,83.0,170.8`. Header chỉ ghi một lần.
- `test_mqtt_payload_accepted_by_server`: payload qua được `valid_sensor_row` của `server_app.py` và có thêm `pm25`. Vì `server_app.py` import streamlit, nên tách `valid_sensor_row` sang `aiot/payload.py` để test và để server dùng lại.
- `test_broker_down_does_not_stop_collection`: publisher raise thì DB và CSV vẫn được ghi.
- `test_all_sensors_fail_writes_nothing_and_survives`
- `test_clock_unsynced_flag_stored`: `clock_synced=False` thì mẫu vẫn được ghi, `clock_synced=0`.
- `test_mq135_ignored_during_warmup`: trong 180 giây đầu sau khi dịch vụ chạy, `mq135_do` là `None`, vì module cần sấy.

- [ ] **Step 2: Chạy test**

Run: `pytest tests/test_collector.py -v`
Expected: FAIL

- [ ] **Step 3: Implement collector**

Vòng lặp giữ nhịp `INTERVAL=10` như client cũ. Gọi `aggregate_pending` mỗi phút; xoá `raw_readings` cũ hơn 7 ngày mỗi ngày một lần. Unit systemd: `Restart=always`, `User=admin`, `WorkingDirectory=/home/admin/BTL_IOT`.

- [ ] **Step 4: Chạy test**

Run: `pytest tests/test_collector.py -v`
Expected: PASS

- [ ] **Step 5: Triển khai lên Pi**

Đặt múi giờ `Asia/Ho_Chi_Minh`. Dừng `pi_sensor_client.py`, vì hai tiến trình không được cùng đọc cảm biến. Sau đó `systemctl enable --now aiot-collector`.

- [ ] **Step 6: Kiểm tra trên Pi sau 1 giờ**

`sqlite3 ~/iot_data/aiot.db "select count(*), sum(pm25 is null), max(ts) from raw_readings; select count(*), sum(valid) from readings;"`
Expected: khoảng 360 mẫu thô, 12 dòng `readings`, phần lớn `valid=1`. `sensors.csv` vẫn tăng; dashboard vẫn hiển thị nếu laptop bật. Khởi động lại Pi và kiểm tra dịch vụ tự chạy lại.

- [ ] **Step 7: Cập nhật trạng thái trong `CLAUDE.md` và mục 7 của sơ đồ, rồi commit**

```bash
git add aiot/collector.py aiot/sensors.py aiot/payload.py server_app.py deploy tests/test_collector.py README.md CLAUDE.md so_do_noi_day_raspberry_pi.md
git commit -m "feat: add collector service for all sensors"
```

### Task 5: Logic điều khiển quạt (hàm thuần)

**Files:**
- Create: `aiot/control.py`, `tests/test_control.py`
- Modify: `CLAUDE.md` (mục Logic điều khiển quạt)

**Interfaces:**
- Consumes: `ControlCfg`
- Produces: `rolling_mean(series, now, window_s, sample_s=10, min_coverage=0.5) -> float | None`, `rolling_median(series, now, window_s, min_samples=3) -> float | None`
- Produces: `Inputs(now, last_sample_ts, danger_level, mean_enter, mean_exit, forecast_3h, forecast_made_at)`
- Produces: `ControlState(mode: "NORMAL" | "PROTECT", entered_at, enter_since, exit_since, fault_active, override_fan: bool | None, override_until: int | None)`, có `to_json()` và `from_json()`
- Produces: `decide(st, inp, cfg) -> tuple[ControlState, list[Event]]`, `fan_on(st, now) -> bool`, `Event(kind, detail)` với `kind` thuộc `fan_on`, `fan_off`, `danger`, `sensor_fault`, `sensor_recovered`, `override_expired`

- [ ] **Step 1: Viết test thất bại**

- `test_enter_after_5_min_above_55`: 299 giây chưa vào, 300 giây vào, `kind == "fan_on"`.
- `test_exactly_55_does_not_enter`, `test_exactly_35_does_not_exit`
- `test_dip_resets_enter_timer`
- `test_exit_after_15_min_below_35_and_held_10_min`
- `test_exit_blocked_until_min_state`: dùng cấu hình có `exit_hold_s` ngắn hơn `min_state_s`.
- `test_danger_enters_immediately_on_1min_median`: trung vị 1 phút ≥ 150 thì vào ngay, không chờ `enter_hold_s`, và có `Event("danger")`.
- `test_single_spike_does_not_trigger_danger`: `rolling_median` của `[20, 20, 900, 20, 20, 20]` là 20 nên không vào.
- `test_danger_needs_three_samples`: chỉ có 2 mẫu thì `rolling_median` trả `None`.
- `test_proactive_enters_on_fresh_forecast`: `forecast_3h=101`, dự báo mới thì vào. `forecast_3h=100` thì không.
- `test_stale_forecast_ignored`: dự báo cũ hơn 5400 giây không có tác dụng.
- `test_exit_blocked_while_forecast_high`, và thoát được khi dự báo hết hạn.
- `test_sensor_gap_holds_state_and_faults_once`: 121 giây không có mẫu thì giữ trạng thái ở cả hai mode, chỉ có một `sensor_fault`, và các bộ đếm thời gian bị xoá.
- `test_recovery_restarts_timers`: có lại dữ liệu thì có `sensor_recovered`, phải chờ đủ thời gian mới đổi trạng thái.
- `test_override_wins_and_expires`: `/fan off` thắng mức khẩn cấp nhưng `danger` vẫn phát; sau 7200 giây trở về tự động, có `override_expired`.
- `test_override_works_during_sensor_gap`
- `test_no_flapping_on_oscillation`: một giờ PM2.5 dao động 30–60 chu kỳ 4 phút cho tối đa 1 lần đổi trạng thái.
- `test_state_json_roundtrip`

- [ ] **Step 2: Chạy test**

Run: `pytest tests/test_control.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `aiot/control.py`**

`decide` không đọc đồng hồ, không đụng I/O. Chỉ dùng `inp.now`.

- [ ] **Step 4: Chạy test**

Run: `pytest tests/test_control.py -v`
Expected: PASS

- [ ] **Step 5: Ghi các luật bổ sung vào `CLAUDE.md` và commit**

```bash
git add aiot/control.py tests/test_control.py CLAUDE.md
git commit -m "feat: add deterministic fan control logic"
```

### Task 6: Dịch vụ controller và relay

**Files:**
- Create: `aiot/relay.py`, `aiot/controller.py`, `deploy/aiot-controller.service`, `scripts/inject_pm.py` (đưa chuỗi PM giả vào DB thử), `tests/test_controller.py`

**Interfaces:**
- Consumes: `decide`, `fan_on`, `rolling_mean`, `pm25_series`, `get_state`, `set_state`, `log_event`
- Produces: `Relay(pin=23)` với `set(on: bool)`, `close()`; `FakeRelay` trong test. `step(conn, relay, cfg, now) -> ControlState`, `restore(conn, relay, cfg, now) -> ControlState`
- Produces: khoá state `control_state` (JSON), `pending_override` (`{"fan": true|false|null, "ttl_s": int}`). `bot` ghi `pending_override`, chỉ `controller` đọc và xoá khoá này.

- [ ] **Step 1: Viết test thất bại với SQLite trong bộ nhớ và `FakeRelay`**

- `test_restart_in_protect_turns_fan_on`
- `test_restart_keeps_entered_at`: thời gian giữ trạng thái không bị đếm lại.
- `test_restart_with_expired_override_returns_to_auto`
- `test_first_boot_defaults_to_normal_fan_off`
- `test_corrupt_state_falls_back_to_normal_and_logs`
- `test_transition_logged_to_events`
- `test_pending_override_applied_then_cleared`
- `test_relay_failure_does_not_persist_wrong_state`: `relay.set` raise thì ghi `events` và lần lặp sau thử lại.

- [ ] **Step 2: Chạy test**

Run: `pytest tests/test_controller.py -v`
Expected: FAIL

- [ ] **Step 3: Implement**

Dùng `lgpio.gpio_claim_output(h, 23, level)` với `level` lấy từ trạng thái đã khôi phục. Nhận SIGTERM thì đưa GPIO23 về LOW, vì khi dịch vụ dừng thì không ai giám sát quạt nữa. Lặp mỗi `loop_s` giây. Import `lgpio` trong `Relay.__init__`. Xem README Phụ lục C nếu `lgpio` lỗi trên Python 3.13.

- [ ] **Step 4: Chạy test**

Run: `pytest tests/test_controller.py -v`
Expected: PASS

- [ ] **Step 5: Kiểm tra trên mạch, có người đứng cạnh, bằng `demo: true`**

Dùng `scripts/inject_pm.py` với một DB thử, không dùng DB thật. Quan sát: quạt quay trong vòng 1 phút sau khi vượt ngưỡng; dừng khi tốt lại; rút nguồn Pi khi đang BẢO VỆ thì quạt quay lại sau khi khởi động; `vcgencmd get_throttled` vẫn là `0x0` khi quạt chạy cùng cả hệ thống.

- [ ] **Step 6: Tắt `demo`, bật dịch vụ thật và commit**

```bash
git add aiot/relay.py aiot/controller.py deploy/aiot-controller.service scripts/inject_pm.py tests/test_controller.py
git commit -m "feat: add controller service driving the fan relay"
```

### Task 7: AQI, cảnh báo và outbox

**Files:**
- Create: `aiot/aqi.py`, `aiot/notifier.py`, `tests/test_aqi.py`, `tests/test_notifier.py`

**Interfaces:**
- Produces: `classify(pm25: float) -> AqiLevel` với `AqiLevel(index: int, label: str)`
- Produces: `in_quiet_hours(hour, start, end) -> bool`, `should_alert(kind, now, conn, cfg, urgent=False) -> bool`, `enqueue(conn, kind, text, now)`, `check_aqi_change(conn, pm25_15min, now, cfg)`, `pending_messages(conn) -> list[OutboxRow]`, `mark_sent(conn, id, now)`, `collapse_stale_alerts(conn, now, max_age_s=3600)`

- [ ] **Step 1: Viết test thất bại**

- `test_aqi_boundaries` (parametrize): 9.0 Tốt, 9.1 Trung bình, 35.4 Trung bình, 35.5 Kém, 55.4 Kém, 55.5 Xấu, 125.4 Xấu, 125.5 Rất xấu, 225.4 Rất xấu, 225.5 Nguy hại.
- `test_aqi_rounds_to_one_decimal`: 9.04 là Tốt, 9.06 là Trung bình; không có khoảng trống giữa hai nhóm.
- `test_aqi_rejects_none_and_negative`
- `test_quiet_wraps_midnight`: với 22–6 thì 23h và 5h là yên lặng; 6h và 21h thì không. `start == end` nghĩa là không có giờ yên lặng.
- `test_same_kind_limited_to_one_per_30_min`: sau 1799 giây bị chặn, sau 1800 giây được gửi. Loại khác không bị ảnh hưởng.
- `test_urgent_bypasses_quiet`
- `test_fault_suppressed_by_quiet_sent_after_if_still_active`: lỗi cảm biến xảy ra trong giờ yên lặng được gửi khi hết giờ nếu vẫn còn lỗi.
- `test_suppressed_change_sent_only_if_still_different`: đổi nhóm rồi quay lại trong thời gian chặn thì không gửi.
- `test_collapse_keeps_reports_and_summarizes_old_alerts`: sau khi mất mạng, 40 cảnh báo cũ gộp thành một tin tóm tắt; báo cáo sáng được giữ nguyên.

- [ ] **Step 2: Chạy test**

Run: `pytest tests/test_aqi.py tests/test_notifier.py -v`
Expected: FAIL

- [ ] **Step 3: Implement, nối Event của controller vào `enqueue`**

Modify `aiot/controller.py`: `danger`, `sensor_fault`, `fan_on`, `fan_off` tạo tin trong outbox; gọi `check_aqi_change` mỗi lần lặp. Thêm test tương ứng vào `tests/test_controller.py`.

- [ ] **Step 4: Chạy test**

Run: `pytest tests/test_aqi.py tests/test_notifier.py tests/test_controller.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiot/aqi.py aiot/notifier.py aiot/controller.py tests
git commit -m "feat: add AQI classification and alert outbox"
```

### Task 8: Telegram bot

**Files:**
- Create: `aiot/telegram_api.py`, `aiot/bot.py`, `deploy/aiot-bot.service`, `tests/test_bot.py`

**Interfaces:**
- Consumes: `pending_messages`, `mark_sent`, `collapse_stale_alerts`, `set_state`, `log_event`, `classify`
- Produces: `TelegramClient(token).get_updates(offset, timeout) -> list[dict]`, `.send(chat_id, text)`; lỗi raise `TelegramError` có thông báo đã che token
- Produces: `handle_update(update, conn, cfg, now) -> str | None`; khoá state `telegram_offset`, `quiet_hours`

- [ ] **Step 1: Viết test thất bại**

- `test_unauthorized_chat_ignored_and_no_reply`: `handle_update` trả `None`, không có `pending_override` trong DB.
- `test_fan_on_writes_pending_override_and_logs`: `bot` không import `aiot.relay`.
- `test_mode_auto_clears_override`, `test_mode_manual_keeps_current_fan_state`
- `test_stale_command_rejected`: lệnh điều khiển có `date` cũ hơn 120 giây không có tác dụng, và trả lời rằng lệnh đã bị bỏ. Lệnh chỉ đọc vẫn được xử lý.
- `test_quiet_validates_input`: `/quiet 22 6` được ghi; `/quiet 25 6` và `/quiet` báo cách dùng.
- `test_now_reports_missing_data`: DB rỗng thì trả lời "chưa có dữ liệu", không raise.
- `test_status_shows_mode_fan_and_data_age`
- `test_unknown_command_gives_help`
- `test_error_message_hides_token`: `requests` raise lỗi có URL chứa token thì `str(TelegramError)` không chứa token.
- `test_offset_persisted_and_advanced_even_on_handler_error`: một tin gây lỗi không làm bot lặp lại mãi.
- `test_send_uses_plain_text`: không có `parse_mode`, vì nội dung từ Gemini có thể làm hỏng Markdown.
- `test_outbox_retries_after_network_failure_and_honors_429`: lỗi mạng thì giữ tin; HTTP 429 thì chờ theo `retry_after`.

- [ ] **Step 2: Chạy test**

Run: `pytest tests/test_bot.py -v`
Expected: FAIL

- [ ] **Step 3: Implement bằng `requests`, long polling**

Làm các lệnh `/now`, `/status`, `/mode`, `/fan`, `/quiet`; tắt log URL của `urllib3`. `/forecast` và `/report` trả lời "chưa sẵn sàng" cho tới Task 9–10.

- [ ] **Step 4: Chạy test**

Run: `pytest tests/test_bot.py -v`
Expected: PASS

- [ ] **Step 5: Kiểm tra thật với bot Telegram của nhóm**

Người dùng tạo bot và tự điền `.env` trên Pi. Thử từng lệnh; `/fan on` làm quạt quay trong vòng `loop_s`. Tắt Wi-Fi 10 phút: quạt và thu dữ liệu vẫn chạy, có mạng lại thì tin được gửi bù. Chat khác không nhận được phản hồi. `journalctl -u aiot-bot | grep -c "<phần đầu token>"` trả về 0.

- [ ] **Step 6: Commit**

```bash
git add aiot/telegram_api.py aiot/bot.py deploy/aiot-bot.service tests/test_bot.py
git commit -m "feat: add Telegram bot with allowlist and outbox delivery"
```

### Task 9: Dự báo đối chứng và tích hợp vào controller

**Files:**
- Create: `aiot/forecast.py`, `deploy/aiot-forecast.service`, `deploy/aiot-forecast.timer`, `tests/test_forecast.py`
- Modify: `aiot/controller.py`, `aiot/bot.py`

**Interfaces:**
- Consumes: bảng `readings`, `classify`
- Produces: `hourly_series(conn, end_ts, hours=48) -> pandas.Series`, `prepare_input(series, hours=24, max_gap_h=2, min_valid=20) -> numpy.ndarray | None`, `persistence(series) -> list[float]`, `seasonal_naive(series) -> list[float]`, `run_forecast(conn, now, predictor=None) -> tuple[str, list[float]]`, `latest_forecast(conn, now, max_age_s) -> tuple[float, int] | None` (trả trung bình 3 giờ đầu và `made_at`)

- [ ] **Step 1: Viết test thất bại**

- `test_hourly_uses_only_valid_rows`
- `test_persistence_repeats_last_value` (6 giá trị)
- `test_seasonal_naive_uses_same_hour_yesterday`
- `test_small_gap_interpolated_large_gap_returns_none`: khoảng trống 2 giờ được nội suy; 3 giờ thì trả `None`.
- `test_cold_start_uses_persistence`: chưa đủ 24 giờ thì `model == "persistence"`.
- `test_no_forecast_from_old_data`: không có dữ liệu, hoặc dữ liệu mới nhất cũ hơn 3 giờ, thì không ghi dự báo và không raise.
- `test_predictor_failure_falls_back`: `predictor` raise thì dùng đối chứng.
- `test_negative_predictions_clipped_to_zero`
- `test_rerun_same_hour_overwrites`: không vi phạm khoá chính.
- `test_latest_forecast_mean_of_first_three_horizons`, `test_stale_forecast_returns_none`

- [ ] **Step 2: Chạy test**

Run: `pytest tests/test_forecast.py -v`
Expected: FAIL

- [ ] **Step 3: Implement, dùng `seasonal_naive` khi `prepare_input` có đủ 24 giờ, ngược lại dùng `persistence`**

Thêm timer chạy mỗi giờ. `controller` đọc `latest_forecast` để điền `Inputs.forecast_3h`. `/forecast` hiển thị 6 giờ tới, nhãn AQI và tên model.

- [ ] **Step 4: Chạy test và kiểm tra `forecasts` trên Pi có dòng mới sau một giờ**

Run: `pytest tests/test_forecast.py tests/test_controller.py tests/test_bot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add aiot/forecast.py aiot/controller.py aiot/bot.py deploy tests
git commit -m "feat: add baseline forecast job and proactive control input"
```

### Task 10: Gemini và báo cáo sáng

**Files:**
- Create: `aiot/advice.py`, `aiot/report.py`, `deploy/aiot-morning.service`, `deploy/aiot-morning.timer`, `tests/test_advice.py`, `tests/test_report.py`
- Modify: `aiot/bot.py`

**Interfaces:**
- Consumes: `latest_forecast`, `classify`, `enqueue`, các bảng `readings`, `events`
- Produces: `summarize(conn, now) -> dict` (24 giờ qua, dự báo, trạng thái quạt), `get_advice(summary, cfg, client=None) -> Advice`, `Advice(headline, advice, source: "gemini" | "template")`, `build_report(conn, now, cfg, client=None) -> str`, `send_morning_report(conn, now, cfg, client=None) -> bool`

- [ ] **Step 1: Kiểm tra model và hạn mức Gemini hiện hành, ghi vào `config.yaml`**

Tra tài liệu chính thức của Google. Không dùng tên model từ trí nhớ.

- [ ] **Step 2: Viết test thất bại với client giả**

- `test_payload_contains_only_aggregates`: JSON gửi đi chỉ có khoá trong danh sách cho phép; không có `chat_id`, `device_id` hay mẫu thô.
- `test_temperature_and_json_mode_requested`: `temperature == 0.2`, kiểu trả về JSON.
- `test_prompt_forbids_diagnosis_and_invented_numbers`
- `test_api_exception_uses_template`, `test_timeout_uses_template`, `test_quota_error_uses_template`
- `test_invalid_json_uses_template`, `test_missing_field_uses_template`, `test_overlong_text_uses_template` (quá 800 ký tự)
- `test_template_exists_for_every_aqi_level`
- `test_report_marks_missing_data`: không có dữ liệu 24 giờ thì báo cáo ghi rõ thiếu dữ liệu, không gọi Gemini và vẫn gửi được.
- `test_report_names_forecast_model`
- `test_morning_report_sent_once_per_day`: chạy lại trong cùng ngày không tạo tin thứ hai.
- `test_daily_call_limit`: vượt `gemini.max_calls_per_day` thì dùng mẫu cố định.

- [ ] **Step 3: Chạy test**

Run: `pytest tests/test_advice.py tests/test_report.py -v`
Expected: FAIL

- [ ] **Step 4: Implement**

Import `google.genai` trong `get_advice`, nên thiếu thư viện cũng rơi về mẫu cố định. Timer `OnCalendar=*-*-* 06:30:00`, `Persistent=true` để chạy bù sau khi mất điện. `/report` gọi `build_report`.

- [ ] **Step 5: Chạy test**

Run: `pytest tests/test_advice.py tests/test_report.py tests/test_bot.py -v`
Expected: PASS

- [ ] **Step 6: Kiểm tra thật**

Gửi `/report` bằng API key thật. Sau đó đặt key sai: bản tin vẫn đến, dùng mẫu cố định.

- [ ] **Step 7: Commit**

```bash
git add aiot/advice.py aiot/report.py aiot/bot.py deploy tests config.yaml
git commit -m "feat: add Gemini advice with fallback and morning report"
```

### Task 11: Chuẩn bị dữ liệu và phân tích theo Chương 12

**Files:**
- Create: `training/data.py`, `training/fetch_public.py`, `training/export_hourly.py`, `training/01_phan_tich_chuoi.ipynb`, `training/requirements.txt`, `tests/test_training_data.py`

**Interfaces:**
- Produces: `chrono_split(df, train=0.7, val=0.15) -> tuple[df, df, df]`, `fit_scaler(train_df, columns) -> MinMaxScaler`, `add_time_features(df)` (sin/cos giờ), `make_windows(df, target="pm25", n_in=24, n_out=6) -> tuple[X, y, target_times]`, `difference(s, lag) -> Series`, `invert_difference(pred, history, lag) -> ndarray`
- Produces: CLI `python training/export_hourly.py --db aiot.db --out hourly.csv` (chỉ dùng dòng `valid=1`)

- [ ] **Step 1: Viết test thất bại**

- `test_split_is_chronological_without_overlap`: `train.index.max() < val.index.min()`.
- `test_scaler_fit_on_train_only`: `scaler.data_max_` bằng max của train dù val có đỉnh lớn hơn.
- `test_window_shapes`: 100 giờ, 3 đặc trưng cho `X.shape == (71, 24, 3)`, `y.shape == (71, 6)`.
- `test_windows_skip_gaps`: thiếu 1 giờ thì mọi cửa sổ chạm giờ đó bị bỏ, không có NaN.
- `test_target_never_inside_input`: giờ đích đầu tiên nằm sau giờ đầu vào cuối cùng.
- `test_difference_roundtrip`: cả lag 1 và lag 24.
- `test_export_excludes_invalid_rows`

- [ ] **Step 2: Chạy test**

Run: `pytest tests/test_training_data.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `training/data.py`, `training/export_hourly.py`**

Viết hàm mới, không dùng lại `normalize()` của notebook Chương 12 vì hàm đó fit trên toàn chuỗi.

- [ ] **Step 4: Chạy test**

Run: `pytest tests/test_training_data.py -v`
Expected: PASS

- [ ] **Step 5: Implement `training/fetch_public.py`**

Lấy PM2.5, gió, mưa theo giờ từ Open-Meteo và dữ liệu công khai của Hà Nội. Lưu rõ nguồn và ngày tải; ghi trong báo cáo đây là dữ liệu dùng để huấn luyện sơ bộ.

- [ ] **Step 6: Lập notebook phân tích, chạy lại từ đầu không lỗi**

Theo đúng thứ tự: chuỗi giờ và trung bình trượt 24 giờ → `diff()` → ACF, ghi nhận có đỉnh lag 24 hay không → sai phân lag 24 → ADF trước và sau. Ghi kết quả thực tế, kể cả khi không thấy đỉnh như kỳ vọng.

- [ ] **Step 7: Commit**

```bash
git add training tests/test_training_data.py
git commit -m "feat: add time-series preparation and Chapter 12 analysis"
```

### Task 12: Huấn luyện, đánh giá và đưa LSTM lên Pi

**Files:**
- Create: `training/train_lstm.py`, `training/evaluate.py`, `training/02_danh_gia.ipynb`, `tests/test_evaluate.py`, `tests/test_tflite_predictor.py`
- Modify: `aiot/forecast.py`

**Interfaces:**
- Consumes: mọi hàm trong `training/data.py`; `run_forecast(..., predictor=...)`
- Produces: `evaluate(y_true, y_pred) -> dict` gồm `mae[h]`, `rmse[h]`, `aqi_accuracy[h]`, `exceedance_recall[h]`; file `pm25_lstm.tflite`, `scaler.json`
- Produces trong `forecast.py`: `load_predictor(model_path, scaler_path) -> Callable | None`

- [ ] **Step 1: Viết test thất bại**

- `test_metrics_on_known_values`: tính tay với mảng nhỏ, theo từng horizon.
- `test_exceedance_recall_when_no_events`: trả `None`, không chia cho 0.
- `test_aqi_accuracy_uses_shared_classifier`: dùng `aiot.aqi.classify`.
- `test_load_predictor_missing_file_returns_none`, `test_load_predictor_corrupt_file_returns_none`
- `test_predictor_shape_mismatch_falls_back`: `scaler.json` không khớp đầu vào thì `run_forecast` dùng đối chứng.
- `test_scaler_json_roundtrip`: kết quả giống `MinMaxScaler` gốc tới `1e-6`.

- [ ] **Step 2: Chạy test**

Run: `pytest tests/test_evaluate.py tests/test_tflite_predictor.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `training/evaluate.py` và `load_predictor`**

Chỉ phụ thuộc NumPy và LiteRT trên Pi, không phụ thuộc TensorFlow hay scikit-learn.

- [ ] **Step 4: Chạy test**

Run: `pytest tests/test_evaluate.py tests/test_tflite_predictor.py -v`
Expected: PASS

- [ ] **Step 5: Implement `training/train_lstm.py`**

LSTM 1–2 lớp, 32–64 đơn vị, đầu ra 6 giá trị, early stopping trên tập validation. So sánh hai dạng đích: giá trị gốc và sai phân lag 24 (có đảo ngược); chọn theo MAE trên validation, không theo test. Đầu vào: PM2.5, nhiệt độ, độ ẩm, sin/cos giờ. Cố định batch size khi xuất. Sau khi xuất, kiểm tra Keras và `.tflite` cho kết quả lệch dưới `1e-3` trên 20 cửa sổ.

- [ ] **Step 6: Đánh giá trên tập test đúng một lần, ghi vào `training/02_danh_gia.ipynb`**

So với persistence, seasonal naive, SARIMA hoặc hồi quy trên đặc trưng trễ, và Open-Meteo Air Quality. Đánh giá MAE, RMSE theo từng horizon, độ chính xác nhóm AQI, độ nhạy với sự kiện vượt ngưỡng. Báo cáo riêng kết quả trên dữ liệu công khai và dữ liệu Pi.

Tiêu chí đưa LSTM lên Pi: vượt persistence ở horizon ≥ 2 giờ. Nếu không đạt, giữ đối chứng cho sản phẩm và báo cáo trung thực.

- [ ] **Step 7: Nếu đạt, chép model lên Pi và kiểm tra**

Run trên Pi: chạy `aiot-forecast.service` một lần, rồi `select model, count(*) from forecasts group by model`.
Expected: có `model = 'lstm'`, thời gian suy luận được ghi lại cho báo cáo. Xoá file model thì lần sau rơi về đối chứng.

- [ ] **Step 8: Commit**

Model chỉ commit khi nhỏ và được nhóm đồng ý.

```bash
git add training aiot/forecast.py tests
git commit -m "feat: add LSTM training, evaluation and on-device inference"
```

### Task 13: Nghiệm thu và tài liệu

**Files:**
- Create: `scripts/acceptance_report.py`, `tests/test_acceptance_report.py`, `docs/nghiem_thu.md`
- Modify: `CLAUDE.md`, `README.md`, `so_do_noi_day_raspberry_pi.md`

**Interfaces:**
- Produces: `valid_ratio(conn, start, end) -> float` (so với số cửa sổ 5 phút kỳ vọng, không phải số dòng đang có), `control_reaction_times(conn, start, end) -> list[float]`, `count_flaps(conn, start, end) -> int`, `report_punctuality(conn, days) -> tuple[int, int]`, `fan_effect(conn, start, end) -> dict`, `station_correlation(local, public) -> dict`

- [ ] **Step 1: Viết test thất bại**

- `test_valid_ratio_counts_missing_windows`: Pi tắt 12 giờ trong 24 giờ thì tỉ lệ khoảng 0,5, không phải 1,0.
- `test_control_reaction_time_from_events`
- `test_count_flaps`
- `test_report_punctuality_counts_6_of_7`: gửi muộn quá mức cho phép thì không tính đúng giờ.
- `test_station_correlation_after_linear_fit`

- [ ] **Step 2: Chạy test**

Run: `pytest tests/test_acceptance_report.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `scripts/acceptance_report.py`**

- [ ] **Step 4: Chạy test**

Run: `pytest tests/test_acceptance_report.py -v`
Expected: PASS

- [ ] **Step 5: Thực hiện bảng kiểm thử trong `CLAUDE.md`, ghi kết quả vào `docs/nghiem_thu.md`**

| Nhóm | Cách làm | Tiêu chí |
| ---- | -------- | -------- |
| Cảm biến | So với trạm công khai ≥ 7 ngày | Tương quan cao sau hiệu chỉnh tuyến tính; ghi kèm độ ẩm |
| Ổn định | Chạy liên tục ≥ 7 ngày | Hợp lệ ≥ 95%, không treo |
| Dự báo | Từ Task 12 | LSTM vượt persistence ở horizon ≥ 2 giờ |
| Điều khiển | Hộp khói, 10 lần vượt ngưỡng, có người trông | Đúng thời gian cấu hình, không nhảy trạng thái |
| Hiệu quả lọc | So đường PM2.5 khi quạt bật và tắt | Có khác biệt đo được |
| Telegram | Báo cáo sáng 7 ngày, có một lần tắt mạng | Đúng giờ ≥ 6/7, có gửi bù |
| Gemini | Giả lập lỗi API | Dùng mẫu cố định, bản tin vẫn gửi |
| An toàn | Dừng `aiot-collector` khi đang chạy; rồi tắt Pi, rút dây PMS7003, bật lại | Cảnh báo lỗi, không bật/tắt bừa |

`CLAUDE.md` ghi test này là "Rút dây PMS7003", nhưng dự án cấm tháo dây khi Pi đang có điện. Vì vậy, để giả lập mất dữ liệu khi đang chạy thì dừng `aiot-collector`; còn trường hợp mất dây thật thì tháo khi đã tắt nguồn.

- [ ] **Step 6: Cập nhật bảng trạng thái trong `CLAUDE.md`, xoá các mục đã chốt, cập nhật README và sơ đồ**

- [ ] **Step 7: Chạy toàn bộ test và commit**

Run: `pytest -v`
Expected: PASS toàn bộ.

```bash
git add scripts tests docs CLAUDE.md README.md so_do_noi_day_raspberry_pi.md
git commit -m "docs: record acceptance results and final status"
```
