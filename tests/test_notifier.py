"""Test chính sách cảnh báo: giờ yên lặng, giới hạn 30 phút, đổi nhóm AQI, outbox."""
from datetime import datetime

import pytest

from aiot.config import Config, TelegramCfg
from aiot.db import connect, get_state, init_schema, set_state
from aiot.notifier import (
    OutboxRow,
    check_aqi_change,
    collapse_stale_alerts,
    enqueue,
    in_quiet_hours,
    mark_sent,
    pending_messages,
    resend_suppressed,
    send_alert,
    should_alert,
)

CFG = Config()                      # giờ yên lặng 22 -> 6, cooldown 1800 s


def at(hour, minute=0, second=0, day=7):
    """Giây epoch của giờ địa phương hour:minute ngày day/11/2026."""
    return int(datetime(2026, 11, day, hour, minute, second).timestamp())


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    yield c
    c.close()


def kinds(conn):
    return [r.kind for r in pending_messages(conn)]


# ---------- giờ yên lặng ----------

def test_quiet_wraps_midnight():
    assert in_quiet_hours(23, 22, 6)
    assert in_quiet_hours(22, 22, 6)
    assert in_quiet_hours(0, 22, 6)
    assert in_quiet_hours(5, 22, 6)
    assert not in_quiet_hours(6, 22, 6)
    assert not in_quiet_hours(21, 22, 6)
    # khoảng không vắt qua nửa đêm
    assert in_quiet_hours(13, 12, 14) and not in_quiet_hours(14, 12, 14)
    # start == end: không có giờ yên lặng
    assert not any(in_quiet_hours(h, 0, 0) for h in range(24))
    assert not any(in_quiet_hours(h, 22, 22) for h in range(24))


def test_should_alert_uses_quiet_hours_from_config(conn):
    assert not should_alert("fan_on", at(23), conn, CFG)
    assert should_alert("fan_on", at(12), conn, CFG)


def test_quiet_hours_state_overrides_config(conn):
    set_state(conn, "quiet_hours", "12,14")         # /quiet 12 14
    assert not should_alert("fan_on", at(13), conn, CFG)
    assert should_alert("fan_on", at(23), conn, CFG)
    set_state(conn, "quiet_hours", "0,0")
    assert should_alert("fan_on", at(23), conn, CFG)


def test_malformed_quiet_hours_state_falls_back_to_config(conn):
    set_state(conn, "quiet_hours", "abc")
    assert not should_alert("fan_on", at(23), conn, CFG)
    assert should_alert("fan_on", at(12), conn, CFG)


def test_accepts_telegram_section_directly(conn):
    assert not should_alert("fan_on", at(23), conn, TelegramCfg())


def test_urgent_bypasses_quiet(conn):
    assert should_alert("danger", at(23), conn, CFG, urgent=True)
    assert send_alert(conn, "danger", "khẩn cấp", at(23), CFG, urgent=True)
    assert kinds(conn) == ["danger"]
    # khẩn cấp vẫn chịu giới hạn 30 phút
    assert not send_alert(conn, "danger", "khẩn cấp", at(23, 10), CFG, urgent=True)
    assert kinds(conn) == ["danger"]


# ---------- giới hạn 1 tin cùng loại mỗi 30 phút ----------

def test_same_kind_limited_to_one_per_30_min(conn):
    t0 = at(12)
    assert send_alert(conn, "fan_on", "bật", t0, CFG)
    assert not should_alert("fan_on", t0 + 1799, conn, CFG)
    assert not send_alert(conn, "fan_on", "bật", t0 + 1799, CFG)
    assert should_alert("fan_off", t0 + 1799, conn, CFG)       # loại khác không bị ảnh hưởng
    assert send_alert(conn, "fan_off", "tắt", t0 + 1799, CFG)
    assert should_alert("fan_on", t0 + 1800, conn, CFG)
    assert send_alert(conn, "fan_on", "bật", t0 + 1800, CFG)
    assert kinds(conn) == ["fan_on", "fan_off", "fan_on"]


def test_suppressed_by_quiet_does_not_start_cooldown(conn):
    assert not send_alert(conn, "fan_on", "bật", at(5, 59), CFG)
    assert send_alert(conn, "fan_on", "bật", at(6), CFG)


def test_send_alert_without_commit_joins_caller_transaction(conn):
    send_alert(conn, "fan_on", "bật", at(12), CFG, commit=False)
    conn.rollback()
    assert pending_messages(conn) == []
    assert should_alert("fan_on", at(12, 1), conn, CFG)


# ---------- lỗi cảm biến bị chặn bởi giờ yên lặng ----------

def test_fault_suppressed_by_quiet_sent_after_if_still_active(conn):
    assert not send_alert(conn, "sensor_fault", "mất dữ liệu", at(23), CFG)
    assert pending_messages(conn) == []
    resend_suppressed(conn, at(2), CFG, fault_active=True)      # vẫn trong giờ yên lặng
    assert pending_messages(conn) == []
    resend_suppressed(conn, at(6), CFG, fault_active=True)      # hết giờ yên lặng, vẫn lỗi
    (row,) = pending_messages(conn)
    assert row.kind == "sensor_fault" and row.created_at == at(6)
    assert "23:00" in row.text                                  # cho biết lỗi từ lúc nào
    resend_suppressed(conn, at(6, 1), CFG, fault_active=True)   # chỉ gửi bù một lần
    assert len(pending_messages(conn)) == 1


def test_fault_recovered_during_quiet_not_resent(conn):
    send_alert(conn, "sensor_fault", "mất dữ liệu", at(23), CFG)
    resend_suppressed(conn, at(1), CFG, fault_active=False)     # có lại dữ liệu lúc 1h
    resend_suppressed(conn, at(6), CFG, fault_active=True)      # lỗi mới chưa được ghi nhận
    assert pending_messages(conn) == []
    assert get_state(conn, "alert_pending:sensor_fault") is None


# ---------- đổi nhóm AQI ----------

def test_first_aqi_reading_recorded_silently(conn):
    check_aqi_change(conn, 20.0, at(12), CFG)
    assert pending_messages(conn) == []
    assert get_state(conn, "aqi_notified") == "1"


def test_aqi_change_enqueued_with_both_labels(conn):
    check_aqi_change(conn, 20.0, at(12), CFG)
    check_aqi_change(conn, 20.5, at(12, 1), CFG)                # cùng nhóm: im lặng
    assert pending_messages(conn) == []
    check_aqi_change(conn, 40.0, at(12, 2), CFG)
    (row,) = pending_messages(conn)
    assert row.kind == "aqi_change"
    assert "Trung bình" in row.text and "Kém" in row.text and "40,0" in row.text
    assert get_state(conn, "aqi_notified") == "2"


def test_aqi_none_is_noop(conn):
    check_aqi_change(conn, None, at(12), CFG)
    assert get_state(conn, "aqi_notified") is None
    assert pending_messages(conn) == []


def test_suppressed_change_sent_only_if_still_different(conn):
    t0 = at(12)
    check_aqi_change(conn, 20.0, t0, CFG)                       # Trung bình
    check_aqi_change(conn, 40.0, t0 + 60, CFG)                  # -> Kém: gửi
    check_aqi_change(conn, 60.0, t0 + 600, CFG)                 # -> Xấu: bị chặn 30 phút
    check_aqi_change(conn, 40.0, t0 + 1200, CFG)                # quay lại Kém trong lúc chặn
    check_aqi_change(conn, 40.0, t0 + 2000, CFG)                # hết chặn, vẫn Kém: không gửi
    assert kinds(conn) == ["aqi_change"]
    check_aqi_change(conn, 60.0, t0 + 2100, CFG)                # hết chặn, khác thật: gửi
    assert kinds(conn) == ["aqi_change", "aqi_change"]


def test_change_during_quiet_sent_after_if_still_different(conn):
    check_aqi_change(conn, 20.0, at(21), CFG)
    check_aqi_change(conn, 40.0, at(23), CFG)                   # giờ yên lặng: chặn
    assert pending_messages(conn) == []
    check_aqi_change(conn, 40.0, at(6), CFG)                    # vẫn khác: gửi
    (row,) = pending_messages(conn)
    assert row.created_at == at(6)


# ---------- outbox ----------

def test_pending_messages_oldest_first_and_mark_sent(conn):
    enqueue(conn, "reply", "b", 200)
    enqueue(conn, "reply", "a", 100)
    rows = pending_messages(conn)
    assert [r.text for r in rows] == ["a", "b"]
    assert isinstance(rows[0], OutboxRow)
    assert rows[0] == OutboxRow(rows[0].id, 100, "reply", "a")
    mark_sent(conn, rows[0].id, 300)
    assert [r.text for r in pending_messages(conn)] == ["b"]
    assert conn.execute("SELECT sent_at FROM outbox WHERE id = ?", (rows[0].id,)).fetchone() == (300,)


def test_collapse_keeps_reports_and_summarizes_old_alerts(conn):
    now = at(12)
    set_state(conn, "aqi_last", "3")                            # mức gần nhất: Xấu
    enqueue(conn, "morning_report", "Báo cáo sáng", at(6, 30))
    for i in range(40):                                         # mất mạng từ 7h tới 10h15
        enqueue(conn, "fan_on" if i % 2 else "aqi_change", f"cảnh báo {i}", at(7) + i * 300)
    enqueue(conn, "reply", "trả lời /now", at(8))
    enqueue(conn, "fan_off", "mới", now - 600)                  # còn mới: giữ nguyên

    collapse_stale_alerts(conn, now, max_age_s=3600)

    rows = pending_messages(conn)
    assert [r.kind for r in rows] == ["morning_report", "reply", "alert_summary", "fan_off"]
    summary = rows[2]
    assert "40" in summary.text and "07:00" in summary.text and "10:15" in summary.text
    assert "Xấu" in summary.text
    assert summary.created_at == at(7) + 39 * 300
    # tin gốc được đánh dấu đã xử lý (không xoá) để còn dấu vết
    n_sent = conn.execute("SELECT COUNT(*) FROM outbox WHERE sent_at = ?", (now,)).fetchone()[0]
    assert n_sent == 40
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 44

    collapse_stale_alerts(conn, now + 1, max_age_s=3600)        # chạy lại không gộp thêm
    assert len(pending_messages(conn)) == 4


def test_collapse_nothing_stale_is_noop(conn):
    enqueue(conn, "fan_on", "bật", at(12))
    collapse_stale_alerts(conn, at(12, 30))
    assert kinds(conn) == ["fan_on"]


def test_relay_error_is_an_alert_kind():
    from aiot.notifier import ALERT_KINDS
    assert "relay_error" in ALERT_KINDS


def test_enqueue_with_photo_path(conn):
    enqueue(conn, "weather_sunny", "nắng", 100, photo_path="/tmp/a.jpg")
    enqueue(conn, "reply", "chữ", 200)
    rows = pending_messages(conn)
    assert [r.photo_path for r in rows] == ["/tmp/a.jpg", None]


def test_stale_weather_alerts_are_collapsed(conn):
    """Tin nắng/mưa cũ hơn 1 giờ (sau khi mất mạng) không còn đúng: gộp vào tin tóm tắt
    (bỏ ảnh); tin còn mới giữ nguyên kèm ảnh."""
    now = at(15)
    enqueue(conn, "weather_sunny", "nắng cũ", at(12), photo_path="/p/a.jpg")
    enqueue(conn, "weather_rain", "mưa mới", now - 600, photo_path="/p/b.jpg")
    collapse_stale_alerts(conn, now)
    rows = pending_messages(conn)
    assert [r.kind for r in rows] == ["alert_summary", "weather_rain"]
    assert rows[0].photo_path is None and "1 cảnh báo" in rows[0].text
    assert rows[1].photo_path == "/p/b.jpg"
