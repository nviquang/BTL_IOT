# Ngưỡng demo cho cảnh báo nắng/mưa: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trình diễn được cảnh báo nắng/mưa kèm ảnh ngay trong phòng, bằng đèn flash điện thoại (khoảng 500 lux), tay che cảm biến và làm ấm/ẩm DHT11, mà không đổi ngưỡng thật.

**Architecture:** Thêm một file cấu hình chỉ chứa mục `weather` với giá trị demo. `aiot.weather` đã có sẵn `--config`, nên không phải sửa code. `config.yaml` và `aiot-weather.timer` giữ nguyên ngưỡng thật; người demo chạy tay `aiot.weather --config deploy/weather_demo.yaml` ngay sau khi một cửa sổ 5 phút khép lại.

**Tech Stack:** Python 3, PyYAML, pytest.

**Spec:** Không có file spec riêng. Yêu cầu từ trao đổi ngày 08/11/2026: 30000 lux không đạt được trong phòng (đèn phòng 215 lux, flash khoảng 500 lux), chỉ cần demo, chưa cần chạy thật.

## Global Constraints

- Không sửa `config.yaml`: `tests/test_config.py::test_repo_config_yaml_weather_matches_defaults` yêu cầu mục `weather` trong đó bằng `WeatherCfg()`.
- Không sửa `WeatherCfg`, `aiot/weather.py`, unit systemd. Không đổi luật quạt.
- File demo nằm trong `deploy/` vì `scripts/up.ps1` đã chép thư mục này lên Pi. Tên không bắt đầu bằng `aiot-` để không bị coi là unit systemd.
- `scripts/up.ps1` chỉ chép bản HEAD, nên file phải được commit mới lên Pi.
- Khi commit chỉ `git add` hai file của plan này. Năm file đang sửa dở (`README.md`, `aiot/collector.py`, `docs/van_hanh_chi_tiet.md`, `server_app.py`, `tests/test_collector.py`) không thuộc plan.
- Giá trị demo:

| Khoá | Thật | Demo | Lý do |
| ---- | ---- | ---- | ----- |
| `sunny_lux` | 30000 | 300 | Giữa đèn phòng (215) và flash (500) |
| `sunny_temp_c` | 33 | 30.1 | Nhóm chốt > 30 °C; code so sánh `>=` nên đặt 30.1 |
| `rain_lux` | 5000 | 50 | Chỉ đạt khi lấy tay che cảm biến, đèn phòng không kích hoạt |
| `rain_hum_pct` | 85 | 80.1 | Nhóm chốt > 80%; code so sánh `>=` nên đặt 80.1 |
| `day_start_h`, `day_end_h` | 8, 16 | 0, 24 | Demo mưa được vào buổi tối |
| `sustain_windows` | 3 | 1 | Chờ 1 cửa sổ 5 phút thay vì 3 |
| `cooldown_s` | 10800 | 240 | Demo lại được sau mỗi cửa sổ 5 phút |

## Review Focus

- Phòng sáng bình thường (215 lux) không được báo nắng, độ ẩm bình thường không được báo mưa khi không che cảm biến.
- Flash chỉ chiếu một phần cửa sổ: trung bình đúng 300 lux thì chưa báo, vì luật dùng phép so sánh `>` trên trung bình 5 phút.
- File demo không được làm dịch vụ trỏ sang DB khác hay đổi ngưỡng quạt.
- Demo trong giờ yên lặng (22h–6h) thì không gửi, người demo cần biết trước.
- Demo lần hai sau 5 phút vẫn phải gửi được tin.

---

### Task 1: File cấu hình demo và test

**Files:**
- Create: `deploy/weather_demo.yaml`
- Test: `tests/test_weather.py` (thêm vào cuối file)

**Interfaces:**
- Consumes: `load_config(path, env_path) -> Config` trong `aiot/config.py`; `weather.run_once(conn, now, cfg, *, capture) -> list[str]`; các helper `at`, `put`, `put_windows`, fixture `conn` có sẵn trong `tests/test_weather.py`.
- Produces: `deploy/weather_demo.yaml`, dùng cho Task 2.

- [ ] **Step 1: Viết test thất bại**

Thêm `from pathlib import Path` và `from aiot.config import load_config` vào đầu file, rồi thêm vào cuối:

```python
# ---------------- cấu hình demo (deploy/weather_demo.yaml) ----------------
ROOT = Path(__file__).resolve().parent.parent
FLASH = dict(lux=500.0, temp=31.0, hum=60.0)
ROOM = dict(lux=215.0, temp=24.0, hum=60.0)
COVERED = dict(lux=5.0, temp=24.0, hum=85.0)


@pytest.fixture
def demo(tmp_path):
    return load_config(ROOT / "deploy" / "weather_demo.yaml", tmp_path / "khong_co.env")


def fake_capture(path, index=0):
    return True


def run_demo(conn, demo, values, now=NOW):
    put_windows(conn, now - 360, values, n=1)   # cửa sổ vừa khép, như timer ở phút 1, 6...
    return weather.run_once(conn, now, demo, capture=fake_capture)


def test_demo_flash_triggers_sunny(conn, demo):
    assert run_demo(conn, demo, FLASH) == ["weather_sunny"]


def test_demo_room_light_triggers_nothing(conn, demo):
    assert run_demo(conn, demo, ROOM) == []


def test_demo_covered_sensor_triggers_rain_in_evening(conn, demo):
    assert run_demo(conn, demo, COVERED, now=at(20, 1)) == ["weather_rain"]


def test_demo_mean_exactly_at_threshold_does_not_trigger(conn, demo):
    assert run_demo(conn, demo, dict(FLASH, lux=300.0)) == []


def test_demo_temp_exactly_30_does_not_trigger_sunny(conn, demo):
    assert run_demo(conn, demo, dict(FLASH, temp=30.0)) == []


def test_demo_humidity_exactly_80_does_not_trigger_rain(conn, demo):
    assert run_demo(conn, demo, dict(COVERED, hum=80.0)) == []


def test_demo_silent_in_quiet_hours(conn, demo):
    assert run_demo(conn, demo, FLASH, now=at(23, 1)) == []


def test_demo_can_repeat_after_five_minutes(conn, demo):
    assert run_demo(conn, demo, FLASH) == ["weather_sunny"]
    assert run_demo(conn, demo, ROOM, now=NOW + 300) == []
    assert run_demo(conn, demo, FLASH, now=NOW + 600) == ["weather_sunny"]


def test_demo_changes_only_weather(tmp_path, demo):
    real = load_config(ROOT / "config.yaml", tmp_path / "khong_co.env")
    assert demo.db_path == real.db_path
    assert demo.control == real.control
    assert demo.telegram == real.telegram
    assert demo.weather.photo_dir == real.weather.photo_dir
    assert demo.weather.camera_index == real.weather.camera_index
```

- [ ] **Step 2: Chạy test, xác nhận thất bại**

Run: `.venv/Scripts/python -m pytest tests/test_weather.py -k demo -v`
Expected: 9 test ERROR với `FileNotFoundError` cho `weather_demo.yaml`.

- [ ] **Step 3: Tạo `deploy/weather_demo.yaml`**

Chỉ có mục `weather` với đúng các giá trị ở cột Demo của bảng trên. Đầu file có chú thích: chỉ dùng để trình diễn trong phòng, không phải ngưỡng thật, chạy bằng lệnh `venv/bin/python -m aiot.weather --config deploy/weather_demo.yaml`.

- [ ] **Step 4: Chạy lại test của file**

Run: `.venv/Scripts/python -m pytest tests/test_weather.py -v`
Expected: tất cả PASS.

- [ ] **Step 5: Chạy toàn bộ test**

Run: `.venv/Scripts/python -m pytest`
Expected: không có test mới thất bại so với trước khi sửa. `test_repo_config_yaml_weather_matches_defaults` vẫn PASS.

- [ ] **Step 6: Commit**

```bash
git add deploy/weather_demo.yaml tests/test_weather.py
git commit -m "feat: add demo thresholds for indoor sunny/rain alert demo"
```

### Task 2: Kịch bản trình diễn (làm khi cần demo, chưa làm bây giờ)

Không có thay đổi code. Cần `aiot-collector` và `aiot-bot` đang chạy, Telegram đã cấu hình, đồng hồ Pi đã đồng bộ NTP, và demo ngoài khung 22h–6h.

- [ ] **Step 1: Đưa code lên Pi**

Run trên laptop: `powershell -ExecutionPolicy Bypass -File scripts\up.ps1`
Expected: Pi có file `~/BTL_IOT/deploy/weather_demo.yaml`.

- [ ] **Step 2: Kiểm tra số đo trong phòng**

Nhắn `/now` cho bot. Expected: lux dưới 300. Ghi lại nhiệt độ và độ ẩm phòng để biết cần làm ấm/ẩm DHT11 thêm bao nhiêu. Thử trước xem cách ở Step 3 và 4 có giữ được trên 30 °C và trên 80% suốt 5 phút không, vì luật dùng trung bình cả cửa sổ.

- [ ] **Step 3: Demo nắng**

Giữ flash cố định vào BH1750 và làm ấm DHT11 (khum tay quanh module, không chạm dây) trọn một cửa sổ 5 phút theo đồng hồ (ví dụ 14:00–14:05). Tới phút 14:06 chạy trên Pi:

```bash
cd ~/BTL_IOT && venv/bin/python -m aiot.weather --config deploy/weather_demo.yaml
```

Expected: Telegram nhận tin "Trời đang nắng gắt" kèm ảnh.

- [ ] **Step 4: Demo mưa**

Lấy tay che BH1750 và giữ khăn ẩm gần DHT11 (không chạm mạch) trọn cửa sổ 5 phút kế tiếp, tới phút 1 sau đó chạy lại lệnh trên. Expected: tin "có thể sắp mưa" kèm ảnh.

- [ ] **Step 5: Ghi rõ trong báo cáo**

Ghi đây là demo với ngưỡng rút gọn. Ngưỡng thật (`config.yaml`) vẫn cần đo ngoài trời mới xác nhận được.
