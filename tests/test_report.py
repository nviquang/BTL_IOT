"""Test báo cáo sáng: tổng hợp 24 giờ, nội dung báo cáo, gửi một lần mỗi ngày, giới hạn số lần
gọi Gemini, chờ đồng bộ giờ. Không dùng mạng (client Gemini giả)."""
import dataclasses
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from aiot import report
from aiot.config import Config, GeminiCfg, Secrets
from aiot.control import ControlState
from aiot.db import connect, get_state, init_schema, log_event, set_state
from aiot.notifier import pending_messages
from aiot.report import build_report, fan_on_seconds, send_morning_report, summarize

NOW = int(datetime(2026, 11, 8, 6, 30, 0).timestamp())
DAY = 86400
CFG = dataclasses.replace(Config(), gemini=GeminiCfg(model="test-model", max_calls_per_day=3),
                          secrets=Secrets(gemini_api_key="fake-key-not-real"))
GOOD = json.dumps({"headline": "Không khí trung bình",
                   "advice": "Người nhạy cảm nên hạn chế ra ngoài lâu."})


class FakeModels:
    def __init__(self, text=GOOD, exc=None):
        self.text, self.exc = text, exc
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append(contents)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(text=self.text)


class FakeClient:
    def __init__(self, **kw):
        self.models = FakeModels(**kw)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    yield c
    c.close()


def fill(conn, start, end, pm25=20.0, temp=27.0, hum=70.0, mq=1, valid=1):
    """Một dòng readings mỗi 5 phút, ts trong [start, end)."""
    conn.executemany(
        "INSERT OR REPLACE INTO readings (ts, pm1, pm25, pm10, temp, hum, lux, mq135_do, valid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(ts, pm25, pm25, pm25, temp, hum, 100.0, mq, valid) for ts in range(start, end, 300)])
    conn.commit()


def full_day(conn, pm25=20.0):
    fill(conn, NOW - DAY, NOW, pm25=pm25)


def put_forecast(conn, made_at, preds, model="seasonal_naive"):
    conn.executemany("INSERT INTO forecasts (made_at, horizon_h, pm25_pred, model) "
                     "VALUES (?, ?, ?, ?)",
                     [(made_at, k + 1, v, model) for k, v in enumerate(preds)])
    conn.commit()


# ---------- tổng hợp ----------

def test_summarize_aggregates(conn):
    start = NOW - DAY
    fill(conn, start, start + 12 * 3600, pm25=5.0)                  # 12 giờ Tốt
    fill(conn, start + 12 * 3600, NOW - 3600, pm25=40.0)            # 11 giờ Kém
    fill(conn, NOW - 3600, NOW, pm25=60.0, mq=0)                    # giờ cuối Xấu, MQ135 vượt
    fill(conn, NOW - 2 * DAY, NOW - DAY, pm25=500.0)                # ngoài 24 giờ: bỏ
    fill(conn, NOW, NOW + 3600, pm25=500.0)                         # "tương lai": bỏ
    conn.execute("UPDATE readings SET valid = 0, pm25 = 999 WHERE ts = ?", (start,))
    conn.commit()
    s = summarize(conn, NOW)
    assert s["has_data"] is True
    assert s["data_coverage"] == pytest.approx(287 / 288, abs=0.01)
    assert s["pm25_min"] == 5.0 and s["pm25_max"] == 60.0
    assert s["pm25_mean"] == pytest.approx((143 * 5 + 132 * 40 + 12 * 60) / 287, abs=0.05)
    assert s["pm25_last_1h_mean"] == 60.0 and s["aqi_now"] == "Xấu"
    assert s["aqi_hours"]["Tốt"] == 12 and s["aqi_hours"]["Kém"] == 11
    assert s["aqi_hours"]["Xấu"] == 1 and sum(s["aqi_hours"].values()) == 24
    assert s["temp_mean"] == 27.0 and s["hum_mean"] == 70.0
    assert s["mq135_exceeded_windows"] == 12
    assert s["forecast"] is None
    assert s["fan_on_now"] is False and s["control_mode"] == "BÌNH THƯỜNG"


def test_summarize_empty(conn):
    s = summarize(conn, NOW)
    assert s["has_data"] is False and s["data_coverage"] == 0
    assert s["pm25_mean"] is None and s["aqi_now"] is None


def test_summarize_has_no_identifiers(conn):
    full_day(conn)
    set_state(conn, "telegram_offset", 123)
    text = json.dumps(summarize(conn, NOW), ensure_ascii=False)
    for bad in ("pi01", "device", "chat", "token", "telegram"):
        assert bad not in text


def test_fan_on_seconds_replays_events(conn):
    t0 = NOW - DAY
    log_event(conn, t0 - 3600, "fan_on", "enter")           # bật từ trước cửa sổ
    log_event(conn, t0 + 1800, "fan_off", "exit")            # 30 phút trong cửa sổ
    log_event(conn, t0 + 7200, "override", f"set fan=on until={t0 + 7200 + 600} (x)")
    # ghi đè hết hạn sau 10 phút (không cần sự kiện)
    log_event(conn, t0 + 10800, "fan_on", "proactive")
    log_event(conn, t0 + 10800 + 300, "override", f"set fan=off until={t0 + 99999} (x)")
    log_event(conn, t0 + 10800 + 900, "override", "cleared (auto)")
    log_event(conn, t0 + 10800 + 1500, "fan_off", "exit")
    # bật 30 + 10 + 5 + 10 phút
    assert fan_on_seconds(conn, t0, NOW) == (30 + 10 + 5 + 10) * 60


def test_summarize_fan_and_forecast(conn):
    full_day(conn)
    log_event(conn, NOW - 3600, "fan_on", "enter")
    set_state(conn, "control_state", ControlState(mode="PROTECT", entered_at=NOW - 3600).to_json())
    put_forecast(conn, NOW // 3600 * 3600, [5, 20, 30, 40, 60, 130])
    s = summarize(conn, NOW)
    assert s["fan_on_now"] is True and s["control_mode"] == "BẢO VỆ"
    assert s["fan_on_minutes_24h"] == 60
    f = s["forecast"]
    assert f["model"] == "seasonal_naive"
    assert f["pm25_next_6h"] == [5, 20, 30, 40, 60, 130]
    assert f["aqi_next_6h"][0] == "Tốt" and f["aqi_next_6h"][-1] == "Rất xấu"
    assert len(f["hours"]) == 6
    # dự báo quá cũ thì bỏ
    assert summarize(conn, NOW + 3 * 3600)["forecast"] is None


# ---------- nội dung báo cáo ----------

def test_report_with_gemini_advice(conn):
    full_day(conn)
    text = build_report(conn, NOW, CFG, client=FakeClient())
    assert "Không khí trung bình" in text
    assert "Người nhạy cảm nên hạn chế ra ngoài lâu." in text
    assert "lời khuyên mẫu" not in text
    assert "20,0 µg/m³" in text and "Trung bình" in text
    assert "08/11/2026" in text
    assert "quạt" in text.lower()
    assert "100%" in text                                   # độ phủ dữ liệu


def test_report_marks_template_advice(conn):
    full_day(conn)
    text = build_report(conn, NOW, CFG, client=FakeClient(exc=RuntimeError("boom")))
    assert "(lời khuyên mẫu)" in text


def test_report_marks_missing_data(conn):
    client = FakeClient()
    text = build_report(conn, NOW, CFG, client=client)
    assert "thiếu dữ liệu" in text.lower()
    assert not client.models.calls
    assert "(lời khuyên mẫu)" in text
    assert send_morning_report(conn, NOW, CFG, client=client) is True
    rows = pending_messages(conn)
    assert len(rows) == 1 and rows[0].kind == "morning_report"
    assert "thiếu dữ liệu" in rows[0].text.lower()
    assert not client.models.calls


def test_report_names_forecast_model(conn):
    full_day(conn)
    text = build_report(conn, NOW, CFG, client=FakeClient())
    assert "chưa có dự báo" in text.lower()
    put_forecast(conn, NOW // 3600 * 3600, [21, 22, 23, 24, 25, 26])
    text = build_report(conn, NOW, CFG, client=FakeClient())
    assert "seasonal_naive" in text and "cùng giờ hôm qua" in text
    assert "26,0 µg/m³" in text


def test_report_mentions_mq135_only_when_exceeded(conn):
    full_day(conn)
    assert "MQ135" not in build_report(conn, NOW, CFG, client=FakeClient())
    conn.execute("UPDATE readings SET mq135_do = 0 WHERE ts >= ?", (NOW - 900,))
    conn.commit()
    text = build_report(conn, NOW, CFG, client=FakeClient())
    assert "MQ135" in text and "3" in text


def test_morning_report_sent_once_per_day(conn):
    full_day(conn)
    client = FakeClient()
    assert send_morning_report(conn, NOW, CFG, client=client) is True
    assert send_morning_report(conn, NOW + 600, CFG, client=client) is False
    rows = pending_messages(conn)
    assert len(rows) == 1 and rows[0].kind == "morning_report"
    assert get_state(conn, "morning_report_date") == "2026-11-08"
    assert len(client.models.calls) == 1                    # lần thứ hai không gọi Gemini
    # hôm sau thì gửi
    fill(conn, NOW, NOW + DAY)
    assert send_morning_report(conn, NOW + DAY, CFG, client=client) is True
    assert len(pending_messages(conn)) == 2


def test_morning_report_write_is_atomic(conn, monkeypatch):
    full_day(conn)

    def broken(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(report, "set_state", broken)
    with pytest.raises(RuntimeError):
        send_morning_report(conn, NOW, CFG, client=FakeClient())
    assert pending_messages(conn) == []
    assert get_state(conn, "morning_report_date") is None


def test_bot_report_does_not_touch_morning_state(conn):
    full_day(conn)
    build_report(conn, NOW, CFG, client=FakeClient())
    assert get_state(conn, "morning_report_date") is None
    assert pending_messages(conn) == []


def test_daily_call_limit(conn):
    full_day(conn)
    client = FakeClient()
    sources = []
    for i in range(5):
        text = build_report(conn, NOW + i, CFG, client=client)
        sources.append("(lời khuyên mẫu)" in text)
    assert len(client.models.calls) == 3
    assert sources == [False, False, False, True, True]
    assert get_state(conn, "gemini_calls:2026-11-08") == "3"
    # sang ngày mới thì được gọi lại
    fill(conn, NOW, NOW + DAY)
    assert "(lời khuyên mẫu)" not in build_report(conn, NOW + DAY, CFG, client=client)
    assert len(client.models.calls) == 4


def test_failed_calls_count_toward_limit(conn):
    full_day(conn)
    client = FakeClient(exc=RuntimeError("quota"))
    for i in range(4):
        build_report(conn, NOW + i, CFG, client=client)
    assert len(client.models.calls) == 3


def test_unsynced_clock_report_sent_with_note(conn):
    full_day(conn)
    set_state(conn, "morning_report_date", "2026-11-08")       # ngày (có thể sai) đã gửi
    assert send_morning_report(conn, NOW, CFG, client=FakeClient(), clock_synced=False) is True
    rows = pending_messages(conn)
    assert len(rows) == 1 and "đồng hồ" in rows[0].text.lower()


def test_report_build_failure_still_sends(conn, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("bug")
    monkeypatch.setattr(report, "build_report", broken)
    assert send_morning_report(conn, NOW, CFG, client=FakeClient()) is True
    rows = pending_messages(conn)
    assert len(rows) == 1 and rows[0].kind == "morning_report"
    assert "/now" in rows[0].text


# ---------- chạy một lần (timer 06:30) ----------

def test_wait_for_clock_polls_until_synced():
    answers = iter([False, False, True])
    slept = []
    clock = {"t": 0.0}

    def sleep(s):
        slept.append(s)
        clock["t"] += s
    assert report.wait_for_clock(lambda: next(answers), sleep=sleep,
                                 monotonic=lambda: clock["t"]) is True
    assert slept == [30, 30]


def test_wait_for_clock_gives_up_after_15_minutes():
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s
    assert report.wait_for_clock(lambda: False, sleep=sleep,
                                 monotonic=lambda: clock["t"]) is False
    assert 900 <= clock["t"] <= 930


def write_cfg(tmp_path):
    db = tmp_path / "aiot.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"db_path: {db.as_posix()}\n", encoding="utf-8")
    return cfg, db


def outbox_texts(db):
    c = connect(db)
    rows = pending_messages(c)
    c.close()
    return rows


def test_main_sends_report(tmp_path, monkeypatch):
    cfg, db = write_cfg(tmp_path)
    monkeypatch.setattr(report, "is_clock_synced", lambda: True)
    assert report.main(["--config", str(cfg), "--env", str(tmp_path / ".env")]) == 0
    rows = outbox_texts(db)
    assert len(rows) == 1 and rows[0].kind == "morning_report"
    assert "đồng hồ" not in rows[0].text.lower()


def test_main_sends_anyway_when_clock_never_syncs(tmp_path, monkeypatch):
    cfg, db = write_cfg(tmp_path)
    monkeypatch.setattr(report, "is_clock_synced", lambda: False)
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s
    assert report.main(["--config", str(cfg), "--env", str(tmp_path / ".env")],
                       sleep=sleep, monotonic=lambda: clock["t"]) == 0
    rows = outbox_texts(db)
    assert len(rows) == 1 and "đồng hồ" in rows[0].text.lower()
