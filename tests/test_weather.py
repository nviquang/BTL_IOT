"""Test luật nắng gắt / có thể mưa từ bảng readings, chính sách cảnh báo và job chạy một lần."""
import dataclasses
import os
from datetime import datetime

import pytest

from aiot import weather
from aiot.config import Config, WeatherCfg
from aiot.db import connect, get_state, init_schema, set_state
from aiot.notifier import pending_messages

CFG = Config()
W = CFG.weather


def at(hour, minute=0, day=7):
    return int(datetime(2026, 11, day, hour, minute).timestamp())


NOW = at(12, 1)                     # timer chạy phút 1, 6, 11...
SUNNY = dict(lux=42000.0, temp=34.6, hum=50.0)
RAINY = dict(lux=1200.0, temp=26.0, hum=91.0)
MILD = dict(lux=10000.0, temp=28.0, hum=70.0)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    yield c
    c.close()


def put(conn, ts, lux=None, temp=None, hum=None, valid=1):
    conn.execute("INSERT OR REPLACE INTO readings (ts, pm25, temp, hum, lux, valid) "
                 "VALUES (?, NULL, ?, ?, ?, ?)", (ts, temp, hum, lux, valid))
    conn.commit()


def put_windows(conn, newest, values, n=3):
    """n cửa sổ 5 phút liền nhau, mới nhất bắt đầu lúc newest."""
    for i in range(n):
        put(conn, newest - i * 300, **values)


# ---------------- luật ----------------
def test_defaults():
    assert W == WeatherCfg()
    assert (W.sunny_lux, W.sunny_temp_c, W.rain_hum_pct, W.rain_lux) == (30000, 33.0, 85.0, 5000)
    assert (W.day_start_h, W.day_end_h, W.sustain_windows, W.cooldown_s) == (8, 16, 3, 10800)
    assert W.photo_dir == "/home/admin/iot_data/photos"
    assert W.photo_keep_days == 7 and W.camera_index == 0


def test_sunny_on_after_three_consecutive_windows(conn):
    put_windows(conn, at(11, 55), SUNNY)
    obs = weather.evaluate(conn, NOW, W)
    assert obs["weather_sunny"].active is True
    assert obs["weather_sunny"].lux == 42000.0 and obs["weather_sunny"].temp == 34.6
    assert obs["weather_rain"].active is False


def test_sunny_needs_both_lux_and_temp(conn):
    put_windows(conn, at(11, 55), dict(SUNNY, temp=32.9))
    assert weather.evaluate(conn, NOW, W)["weather_sunny"].active is False
    conn.execute("DELETE FROM readings")
    put_windows(conn, at(11, 55), dict(SUNNY, lux=30000.0))      # cần lớn hơn hẳn
    assert weather.evaluate(conn, NOW, W)["weather_sunny"].active is False


def test_temp_at_threshold_counts_as_sunny(conn):
    put_windows(conn, at(11, 55), dict(SUNNY, temp=33.0))
    assert weather.evaluate(conn, NOW, W)["weather_sunny"].active is True


def test_one_window_breaking_condition_turns_off(conn):
    put_windows(conn, at(11, 55), SUNNY)
    put(conn, at(11, 50), **MILD)
    assert weather.evaluate(conn, NOW, W)["weather_sunny"].active is False


def test_gap_in_windows_means_unknown(conn):
    put(conn, at(11, 55), **SUNNY)
    put(conn, at(11, 50), **SUNNY)
    put(conn, at(11, 40), **SUNNY)                              # thiếu cửa sổ 11:45
    obs = weather.evaluate(conn, NOW, W)
    assert obs["weather_sunny"] is None and obs["weather_rain"] is None


def test_too_few_or_stale_windows_means_unknown(conn):
    put_windows(conn, at(11, 55), SUNNY, n=2)
    assert weather.evaluate(conn, NOW, W)["weather_sunny"] is None
    conn.execute("DELETE FROM readings")
    put_windows(conn, at(11, 45), SUNNY)                         # mới nhất cách 16 phút
    assert weather.evaluate(conn, NOW, W)["weather_sunny"] is None


def test_windows_missing_needed_fields_are_skipped(conn):
    put_windows(conn, at(11, 55), SUNNY)
    put(conn, at(11, 50), lux=None, temp=34.0, hum=50.0)          # BH1750 lỗi ở cửa sổ này
    assert weather.evaluate(conn, NOW, W)["weather_sunny"] is None


def test_invalid_pm_window_still_used_for_weather(conn):
    put_windows(conn, at(11, 55), dict(SUNNY, valid=0))
    assert weather.evaluate(conn, NOW, W)["weather_sunny"].active is True


def test_rain_likely_in_daytime_only(conn):
    put_windows(conn, at(14, 55), RAINY)
    obs = weather.evaluate(conn, at(15, 1), W)
    assert obs["weather_rain"].active is True
    assert obs["weather_rain"].hum == 91.0 and obs["weather_rain"].lux == 1200.0
    assert obs["weather_sunny"].active is False
    conn.execute("DELETE FROM readings")
    put_windows(conn, at(18, 0), RAINY)
    assert weather.evaluate(conn, at(18, 6), W)["weather_rain"].active is False   # tối: lux thấp
    conn.execute("DELETE FROM readings")
    put_windows(conn, at(5, 50), RAINY)
    assert weather.evaluate(conn, at(5, 56), W)["weather_rain"].active is False


def test_rain_thresholds(conn):
    put_windows(conn, at(11, 55), dict(RAINY, hum=84.9))
    assert weather.evaluate(conn, NOW, W)["weather_rain"].active is False
    conn.execute("DELETE FROM readings")
    put_windows(conn, at(11, 55), dict(RAINY, lux=5000.0))
    assert weather.evaluate(conn, NOW, W)["weather_rain"].active is False


# ---------------- tin nhắn ----------------
def test_messages_use_comma_decimal_and_disclaimer():
    sunny = weather.message("weather_sunny", weather.Observation(True, 42000.4, 34.56, 50.0))
    assert sunny.startswith("Trời đang nắng gắt (42000 lux, 34,6 °C). "
                            "Ra ngoài nhớ mang áo chống nắng.")
    rain = weather.message("weather_rain", weather.Observation(True, 1200.0, 26.0, 90.6))
    assert rain.startswith("Trời tối và ẩm (91%, 1200 lux), có thể sắp mưa. Nhớ mang áo mưa.")
    for text in (sunny, rain):
        second = text.split("\n")[1]
        assert "ước lượng" in second and "dự báo thời tiết" in second


# ---------------- chạy một lần ----------------
class FakeCamera:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, path, index=0, **kw):
        self.calls.append((path, index))
        if self.ok:                                     # như capture_jpeg: tạo thư mục cha
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"jpeg")
        return self.ok


def cfg_with(tmp_path, **kw):
    return dataclasses.replace(
        CFG, weather=dataclasses.replace(W, photo_dir=str(tmp_path / "photos"), **kw))


def test_run_once_alerts_with_photo(conn, tmp_path):
    cfg = cfg_with(tmp_path, camera_index=1)
    put_windows(conn, at(11, 55), SUNNY)
    cam = FakeCamera()
    assert weather.run_once(conn, NOW, cfg, capture=cam) == ["weather_sunny"]
    (row,) = pending_messages(conn)
    assert row.kind == "weather_sunny" and row.text.startswith("Trời đang nắng gắt")
    assert row.photo_path == cam.calls[0][0]
    assert cam.calls[0][1] == 1
    assert row.photo_path.startswith(str(tmp_path / "photos"))
    assert "20261107_120100" in row.photo_path and "weather_sunny" in row.photo_path
    assert get_state(conn, "weather_state:weather_sunny") == "1"
    assert get_state(conn, "alert_last:weather_sunny") == str(NOW)


def test_run_once_capture_failure_sends_text(conn, tmp_path):
    put_windows(conn, at(11, 55), SUNNY)
    weather.run_once(conn, NOW, cfg_with(tmp_path), capture=FakeCamera(ok=False))
    (row,) = pending_messages(conn)
    assert row.photo_path is None
    assert row.text.endswith(" (không chụp được ảnh)")


def test_run_once_capture_exception_sends_text(conn, tmp_path):
    def broken(path, index=0, **kw):
        raise RuntimeError("cv2 hỏng")
    put_windows(conn, at(11, 55), SUNNY)
    weather.run_once(conn, NOW, cfg_with(tmp_path), capture=broken)
    (row,) = pending_messages(conn)
    assert row.photo_path is None and "không chụp được ảnh" in row.text


def test_alert_only_on_transition(conn, tmp_path):
    cfg = cfg_with(tmp_path)
    cam = FakeCamera()
    put_windows(conn, at(11, 55), SUNNY)
    weather.run_once(conn, NOW, cfg, capture=cam)
    put(conn, at(12, 0), **SUNNY)
    assert weather.run_once(conn, NOW + 300, cfg, capture=cam) == []      # vẫn nắng: không báo lại
    assert len(pending_messages(conn)) == 1 and len(cam.calls) == 1


def test_unknown_keeps_previous_state(conn, tmp_path):
    cfg = cfg_with(tmp_path)
    set_state(conn, "weather_state:weather_sunny", "1")
    assert weather.run_once(conn, NOW, cfg, capture=FakeCamera()) == []   # không có dữ liệu
    assert get_state(conn, "weather_state:weather_sunny") == "1"


def test_cooldown_three_hours_per_kind(conn, tmp_path):
    cfg = cfg_with(tmp_path)
    cam = FakeCamera()
    put_windows(conn, at(11, 55), SUNNY)
    weather.run_once(conn, NOW, cfg, capture=cam)
    # hết nắng rồi nắng lại sau 1 giờ: chưa hết 3 giờ, không báo
    put_windows(conn, at(12, 25), MILD)
    weather.run_once(conn, at(12, 31), cfg, capture=cam)
    assert get_state(conn, "weather_state:weather_sunny") == "0"
    put_windows(conn, at(12, 55), SUNNY)
    assert weather.run_once(conn, at(13, 1), cfg, capture=cam) == []
    assert len(cam.calls) == 1
    # sau 3 giờ kể từ tin trước, vẫn nắng và chưa báo lần này: báo
    put_windows(conn, at(14, 55), SUNNY)
    assert weather.run_once(conn, at(15, 1), cfg, capture=cam) == ["weather_sunny"]
    assert len(pending_messages(conn)) == 2


def test_quiet_hours_suppress_then_alert_when_allowed(conn, tmp_path):
    cfg = cfg_with(tmp_path)
    set_state(conn, "quiet_hours", "11,13")                 # /quiet 11 13
    cam = FakeCamera()
    put_windows(conn, at(11, 55), SUNNY)
    assert weather.run_once(conn, NOW, cfg, capture=cam) == []
    assert cam.calls == [] and pending_messages(conn) == []
    assert get_state(conn, "weather_state:weather_sunny") != "1"
    put_windows(conn, at(12, 55), SUNNY)
    assert weather.run_once(conn, at(13, 1), cfg, capture=cam) == ["weather_sunny"]


def test_kinds_are_independent(conn, tmp_path):
    cfg = cfg_with(tmp_path)
    set_state(conn, "alert_last:weather_sunny", NOW - 60)
    put_windows(conn, at(11, 55), RAINY)
    assert weather.run_once(conn, NOW, cfg, capture=FakeCamera()) == ["weather_rain"]


def test_main_skips_when_clock_untrusted(tmp_path, monkeypatch):
    cfg = dataclasses.replace(cfg_with(tmp_path), db_path=str(tmp_path / "aiot.db"))
    monkeypatch.setattr(weather, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(weather, "is_clock_trusted", lambda: False)
    called = []
    monkeypatch.setattr(weather, "run_once", lambda *a, **k: called.append(1))
    assert weather.main([]) == 0
    assert called == []


def test_main_runs_and_purges(tmp_path, monkeypatch):
    cfg = dataclasses.replace(cfg_with(tmp_path), db_path=str(tmp_path / "aiot.db"))
    monkeypatch.setattr(weather, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(weather, "is_clock_trusted", lambda: True)
    ran, purged = [], []
    monkeypatch.setattr(weather, "run_once", lambda conn, now, c, **k: ran.append(now) or [])
    monkeypatch.setattr(weather, "purge_photos",
                        lambda d, days, now: purged.append((d, days)) or 0)
    assert weather.main([]) == 0
    assert len(ran) == 1 and purged == [(cfg.weather.photo_dir, 7)]


def test_main_exits_zero_on_error(tmp_path, monkeypatch):
    cfg = dataclasses.replace(cfg_with(tmp_path), db_path=str(tmp_path / "aiot.db"))
    monkeypatch.setattr(weather, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(weather, "is_clock_trusted", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("lỗi")
    monkeypatch.setattr(weather, "run_once", boom)
    assert weather.main([]) == 0


def test_rain_not_at_dawn_or_dusk(conn):
    """Bình minh ~06:10, hoàng hôn ~17:15 (Hà Nội tháng 11): lux thấp + ẩm không phải mưa."""
    put_windows(conn, at(16, 0), RAINY)
    assert weather.evaluate(conn, at(16, 6), W)["weather_rain"].active is False
    conn.execute("DELETE FROM readings")
    put_windows(conn, at(7, 50), RAINY)
    assert weather.evaluate(conn, at(7, 56), W)["weather_rain"].active is False


def test_default_capture_is_subprocess():
    import inspect
    from aiot import camera
    assert inspect.signature(weather.run_once).parameters["capture"].default \
        is camera.capture_subprocess


def test_remembered_on_state_expires_over_data_gap(conn, tmp_path):
    """Trạng thái "bật" đã nhớ nhưng mất dữ liệu lâu hơn cooldown_s: coi như chưa biết,
    điều kiện quay lại thì báo lại."""
    cfg = cfg_with(tmp_path)
    cam = FakeCamera()
    put_windows(conn, at(11, 55), SUNNY)
    weather.run_once(conn, NOW, cfg, capture=cam)
    assert get_state(conn, "weather_seen:weather_sunny") == str(NOW)
    # collector dừng 4 giờ (không có cửa sổ nào), rồi nắng lại
    put_windows(conn, at(15, 55), SUNNY)
    assert weather.run_once(conn, at(16, 1), cfg, capture=cam) == ["weather_sunny"]
    assert len(cam.calls) == 2


def test_continuous_state_does_not_expire(conn, tmp_path):
    cfg = cfg_with(tmp_path)
    cam = FakeCamera()
    put_windows(conn, at(11, 55), SUNNY)
    weather.run_once(conn, NOW, cfg, capture=cam)
    t = NOW
    for i in range(1, 50):                              # nắng liên tục hơn 4 giờ
        put(conn, at(11, 55) + i * 300, **SUNNY)
        t = NOW + i * 300
        assert weather.run_once(conn, t, cfg, capture=cam) == []
    assert len(cam.calls) == 1


def test_stale_on_state_expiry_persists_through_quiet_hours(conn, tmp_path):
    """"Bật" cũ bị bỏ trong giờ yên lặng phải được ghi xuống DB, để lần chạy được phép sau
    vẫn báo."""
    cfg = cfg_with(tmp_path)
    cam = FakeCamera()
    set_state(conn, "weather_state:weather_sunny", "1")
    set_state(conn, "weather_seen:weather_sunny", at(7))       # cũ hơn 3 giờ
    set_state(conn, "quiet_hours", "11,13")
    put_windows(conn, at(11, 55), SUNNY)
    assert weather.run_once(conn, NOW, cfg, capture=cam) == []  # đang giờ yên lặng
    assert get_state(conn, "weather_state:weather_sunny") != "1"
    put_windows(conn, at(12, 55), SUNNY)
    assert weather.run_once(conn, at(13, 1), cfg, capture=cam) == ["weather_sunny"]
