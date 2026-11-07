"""Test bot Telegram: allowlist, lệnh, lệnh cũ, che token, offset, gửi outbox. Không dùng mạng."""
import dataclasses
import json
import logging
import sys
from datetime import datetime

import pytest
import requests

from aiot import bot
from aiot.config import Config, Secrets
from aiot.control import ControlState
from aiot.db import RawSample, connect, get_state, init_schema, insert_raw, set_state
from aiot.notifier import enqueue, pending_messages
from aiot.telegram_api import TelegramClient, TelegramError, install_log_redaction

TOKEN = "123456789:AAH-secret_TokenValue-xyz"
CHAT = 111
OTHER = 999
NOW = int(datetime(2026, 11, 7, 14, 0, 0).timestamp())
CFG = dataclasses.replace(
    Config(), secrets=Secrets(telegram_token=TOKEN, allowed_chat_ids=frozenset({CHAT, 222})))


@pytest.fixture(autouse=True)
def clock_synced(monkeypatch):
    """Mặc định đồng hồ Pi đã đồng bộ NTP (máy test không có timedatectl)."""
    monkeypatch.setattr(bot, "is_clock_trusted", lambda: True)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    yield c
    c.close()


def upd(text, chat=CHAT, date=NOW, uid=1000):
    return {"update_id": uid,
            "message": {"message_id": 5, "date": date, "text": text,
                        "chat": {"id": chat, "type": "private"}}}


def handle(conn, text, **kw):
    now = kw.pop("now", NOW)
    return bot.handle_update(upd(text, **kw), conn, CFG, now)


def pending(conn):
    text = get_state(conn, "pending_override")
    return None if text is None else json.loads(text)


def command_events(conn):
    return [r[0] for r in conn.execute(
        "SELECT detail FROM events WHERE kind = 'command' ORDER BY rowid")]


def add_sample(conn, ts, pm25=20.0, mq=1):
    insert_raw(conn, RawSample(ts, 12.0, pm25, 30.0, 27.5, 70.0, 150.0, mq, True))


# ---------------- allowlist ----------------
def test_unauthorized_chat_ignored_and_no_reply(conn):
    for text in ("/fan on", "/now", "/status", "xin chào", "/quiet 22 6"):
        assert handle(conn, text, chat=OTHER) is None
    assert pending(conn) is None
    assert get_state(conn, "quiet_hours") is None
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_update_without_message_or_text_ignored(conn):
    assert bot.handle_update({"update_id": 1}, conn, CFG, NOW) is None
    no_text = upd("x")
    del no_text["message"]["text"]
    assert bot.handle_update(no_text, conn, CFG, NOW) is None


# ---------------- lệnh điều khiển ----------------
def test_fan_on_writes_pending_override_and_logs(conn):
    reply = handle(conn, "/fan on", uid=4242)
    assert pending(conn) == {"fan": True, "ttl_s": CFG.control.override_ttl_s, "id": 4242, "at": NOW}
    assert "bật" in reply
    events = command_events(conn)
    assert len(events) == 1 and "/fan on" in events[0] and str(CHAT) in events[0]
    assert TOKEN not in events[0]
    # bot không bao giờ chạm GPIO: không import aiot.relay hay thư viện GPIO
    imported = _imported_modules(bot) | _imported_modules(sys.modules["aiot.telegram_api"])
    assert not imported & {"aiot.relay", "lgpio", "gpiozero", "RPi.GPIO"}


def _imported_modules(module):
    import ast
    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_fan_off_and_bot_suffix(conn):
    handle(conn, "/fan@TramKhongKhiBot OFF", uid=7)
    assert pending(conn) == {"fan": False, "ttl_s": CFG.control.override_ttl_s, "id": 7, "at": NOW}


def test_identical_consecutive_commands_are_distinct_texts(conn):
    handle(conn, "/fan on", uid=1)
    first = get_state(conn, "pending_override")
    handle(conn, "/fan on", uid=2)
    assert get_state(conn, "pending_override") != first


def test_fan_without_valid_argument_shows_usage(conn):
    for text in ("/fan", "/fan maybe"):
        reply = handle(conn, text)
        assert "/fan on" in reply and "/fan off" in reply
    assert pending(conn) is None


def test_mode_auto_clears_override(conn):
    handle(conn, "/mode auto", uid=9)
    assert pending(conn) == {"fan": None, "id": 9, "at": NOW}
    assert any("/mode auto" in d for d in command_events(conn))


def test_mode_manual_keeps_current_fan_state(conn):
    # chưa có trạng thái: BÌNH THƯỜNG -> quạt tắt
    handle(conn, "/mode manual", uid=1)
    assert pending(conn) == {"fan": False, "ttl_s": CFG.control.override_ttl_s, "id": 1, "at": NOW}
    # đang BẢO VỆ -> giữ quạt bật
    set_state(conn, "control_state", ControlState(mode="PROTECT", entered_at=NOW - 60).to_json())
    handle(conn, "/mode manual", uid=2)
    assert pending(conn)["fan"] is True
    # BẢO VỆ nhưng ghi đè tắt còn hạn -> quạt đang tắt
    set_state(conn, "control_state", ControlState(
        mode="PROTECT", override_fan=False, override_until=NOW + 600).to_json())
    handle(conn, "/mode manual", uid=3)
    assert pending(conn)["fan"] is False


def test_mode_without_valid_argument_shows_usage(conn):
    reply = handle(conn, "/mode turbo")
    assert "/mode auto" in reply and pending(conn) is None


def test_stale_command_rejected(conn):
    old = NOW - CFG.telegram.stale_command_s - 1
    for text in ("/fan on", "/mode auto", "/quiet 22 6"):
        reply = handle(conn, text, date=old)
        assert "bỏ" in reply.lower()
    assert pending(conn) is None
    assert get_state(conn, "quiet_hours") is None
    # lệnh chỉ đọc vẫn được trả lời
    for text in ("/now", "/status", "/help", "/forecast", "/report"):
        assert handle(conn, text, date=old)
    # đúng ngưỡng thì vẫn áp dụng
    handle(conn, "/fan on", date=NOW - CFG.telegram.stale_command_s)
    assert pending(conn)["fan"] is True


def test_quiet_validates_input(conn):
    reply = handle(conn, "/quiet 22 6")
    assert get_state(conn, "quiet_hours") == "22,6"
    assert "22" in reply and "6" in reply
    assert any("/quiet 22 6" in d for d in command_events(conn))
    for text in ("/quiet 25 6", "/quiet", "/quiet 22", "/quiet a b", "/quiet -1 6"):
        reply = handle(conn, text)
        assert "/quiet" in reply and "0" in reply      # cách dùng
        assert get_state(conn, "quiet_hours") == "22,6"


# ---------------- lệnh chỉ đọc ----------------
def test_now_reports_missing_data(conn):
    reply = handle(conn, "/now")
    assert "chưa có dữ liệu" in reply.lower()


def test_now_reports_old_data_with_age(conn):
    add_sample(conn, NOW - 20 * 60)
    reply = handle(conn, "/now")
    assert "chưa có dữ liệu" in reply.lower()
    assert "20 phút" in reply


def test_now_reports_latest_sample(conn):
    add_sample(conn, NOW - 100, pm25=40.0)
    add_sample(conn, NOW - 30, pm25=60.0, mq=0)
    reply = handle(conn, "/now")
    assert "60,0" in reply and "Xấu" in reply           # PM2.5 và nhãn AQI
    assert "12,0" in reply and "30,0" in reply           # PM1, PM10
    assert "27,5" in reply and "70" in reply and "150" in reply
    assert "vượt ngưỡng" in reply.lower()
    assert "co2" not in reply.lower() and "co₂" not in reply.lower()
    assert "30 giây" in reply


def test_now_handles_missing_values(conn):
    insert_raw(conn, RawSample(NOW - 10, None, None, None, 26.0, None, None, None, True))
    reply = handle(conn, "/now")
    assert "26,0" in reply
    assert "PM2.5" in reply


def test_status_shows_mode_fan_and_data_age(conn):
    add_sample(conn, NOW - 45)
    set_state(conn, "control_state", ControlState(mode="PROTECT", entered_at=NOW - 600).to_json())
    reply = handle(conn, "/status")
    assert "BẢO VỆ" in reply
    assert "bật" in reply
    assert "45 giây" in reply
    assert "22" in reply and "6" in reply                # giờ yên lặng mặc định


def test_status_shows_override_fault_and_custom_quiet(conn):
    until = NOW + 3600
    set_state(conn, "control_state", ControlState(
        mode="NORMAL", fault_active=True, override_fan=True, override_until=until).to_json())
    set_state(conn, "quiet_hours", "23,7")
    reply = handle(conn, "/status")
    assert "BÌNH THƯỜNG" in reply
    assert datetime.fromtimestamp(until).strftime("%H:%M") in reply
    assert "lỗi" in reply.lower()
    assert "23" in reply and "7" in reply
    assert "chưa có dữ liệu" in reply.lower()


def test_status_without_state_defaults_normal(conn):
    reply = handle(conn, "/status")
    assert "BÌNH THƯỜNG" in reply and "tắt" in reply


def test_report_uses_build_report(conn, monkeypatch):
    calls = []

    def fake_build(c, now, cfg, client=None):
        calls.append(now)
        return "BÁO CÁO"
    monkeypatch.setattr(bot, "build_report", fake_build)
    assert handle(conn, "/report") == "BÁO CÁO"
    assert calls == [NOW]
    assert pending_messages(conn) == []                 # trả lời trực tiếp, không qua outbox


def test_report_without_gemini_uses_template(conn):
    # CFG không có model/API key: không gọi mạng, báo thiếu dữ liệu, lời khuyên mẫu
    reply = handle(conn, "/report")
    assert "thiếu dữ liệu" in reply.lower() and "(lời khuyên mẫu)" in reply
    assert get_state(conn, "morning_report_date") is None


def put_forecast(conn, made_at, preds, model="seasonal_naive"):
    conn.executemany("INSERT INTO forecasts VALUES (?, ?, ?, ?)",
                     [(made_at, k + 1, v, model) for k, v in enumerate(preds)])
    conn.commit()


def test_forecast_none_yet(conn):
    assert "chưa có dự báo" in handle(conn, "/forecast").lower()


def test_forecast_shows_six_hours_with_aqi_and_model(conn):
    made_at = NOW // 3600 * 3600
    put_forecast(conn, made_at - 3600, [1.0] * 6, model="persistence")     # cũ hơn: không hiện
    put_forecast(conn, made_at, [5.0, 12.34, 40.0, 60.0, 130.0, 230.0])
    reply = handle(conn, "/forecast", now=made_at + 1200)
    for k in range(6):
        assert datetime.fromtimestamp(made_at + k * 3600).strftime("%H:00") in reply
    for text in ("5,0", "12,3", "40,0", "60,0", "130,0", "230,0"):
        assert text in reply
    for label in ("Tốt", "Trung bình", "Kém", "Xấu", "Rất xấu", "Nguy hại"):
        assert label in reply
    assert "seasonal_naive" in reply and "persistence" not in reply
    assert "20 phút" in reply                                               # tuổi dự báo
    assert "12.3" not in reply


def test_forecast_too_old_not_shown(conn):
    made_at = NOW // 3600 * 3600
    put_forecast(conn, made_at, [20.0] * 6)
    reply = handle(conn, "/forecast", now=made_at + CFG.control.forecast_max_age_s + 1)
    assert "chưa có dự báo" in reply.lower()
    assert "20,0" not in reply


def test_unknown_command_gives_help(conn):
    for text in ("/abc", "xin chào", "/help", "/start"):
        reply = handle(conn, text)
        for cmd in ("/now", "/status", "/fan", "/mode", "/quiet"):
            assert cmd in reply


# ---------------- TelegramClient ----------------
class FakeResponse:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload if payload is not None else {"ok": True, "result": []}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def test_error_message_hides_token():
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    err = requests.exceptions.ConnectionError(
        f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: "
        f"/bot{TOKEN}/getUpdates (Caused by NewConnectionError) {url}")
    client = TelegramClient(TOKEN, session=FakeSession([err]))
    with pytest.raises(TelegramError) as info:
        client.get_updates(None, 50)
    assert TOKEN not in str(info.value)
    assert TOKEN not in repr(info.value)
    assert "<token>" in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_error_message_hides_url_encoded_token():
    from urllib.parse import quote
    err = requests.exceptions.ReadTimeout(f"timeout /bot{quote(TOKEN, safe='')}/sendMessage")
    client = TelegramClient(TOKEN, session=FakeSession([err]))
    with pytest.raises(TelegramError) as info:
        client.send(CHAT, "hi")
    assert quote(TOKEN, safe="") not in str(info.value)


def test_log_redaction_filter_hides_token():
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    cap = Capture()
    logger = logging.getLogger("aiot.test_redact")
    logger.addHandler(cap)
    logger.propagate = False
    try:
        install_log_redaction(TOKEN, handlers=[cap])
        logger.warning("url là %s", f"https://api.telegram.org/bot{TOKEN}/x")
        try:
            raise RuntimeError(f"lỗi với {TOKEN}")
        except RuntimeError:
            logger.exception("có lỗi")
    finally:
        logger.removeHandler(cap)
    assert records and all(TOKEN not in r for r in records)
    assert "<token>" in records[0]


def test_urllib3_logging_silenced():
    bot.configure_logging()
    assert logging.getLogger("urllib3").getEffectiveLevel() >= logging.WARNING


def test_get_updates_uses_long_polling_and_offset():
    session = FakeSession([FakeResponse(payload={"ok": True, "result": [{"update_id": 5}]})])
    client = TelegramClient(TOKEN, session=session)
    assert client.get_updates(42, 50) == [{"update_id": 5}]
    call = session.calls[0]
    assert call["url"].endswith("/getUpdates")
    assert call["json"]["timeout"] == 50 and call["json"]["offset"] == 42
    assert call["timeout"] > 50                       # timeout HTTP dài hơn long polling


def test_send_uses_plain_text():
    session = FakeSession([FakeResponse(payload={"ok": True, "result": {}})])
    client = TelegramClient(TOKEN, session=session)
    client.send(CHAT, "*không phải* _markdown_ [x](y)")
    call = session.calls[0]
    assert call["url"].endswith("/sendMessage")
    assert call["json"]["chat_id"] == CHAT
    assert call["json"]["text"] == "*không phải* _markdown_ [x](y)"
    assert "parse_mode" not in call["json"]


def test_send_truncates_long_text():
    session = FakeSession([FakeResponse(payload={"ok": True, "result": {}})])
    TelegramClient(TOKEN, session=session).send(CHAT, "a" * 5000)
    assert len(session.calls[0]["json"]["text"]) <= 4096


def test_http_429_carries_retry_after():
    session = FakeSession([FakeResponse(429, {"ok": False, "error_code": 429,
                                              "description": "Too Many Requests",
                                              "parameters": {"retry_after": 7}})])
    with pytest.raises(TelegramError) as info:
        TelegramClient(TOKEN, session=session).send(CHAT, "x")
    assert info.value.retry_after == 7 and info.value.status == 429


def test_api_error_and_bad_json_raise_telegram_error():
    session = FakeSession([FakeResponse(403, {"ok": False, "error_code": 403,
                                              "description": "Forbidden: bot was blocked"}),
                           FakeResponse(502, bad_json=True)])
    client = TelegramClient(TOKEN, session=session)
    with pytest.raises(TelegramError) as info:
        client.send(CHAT, "x")
    assert info.value.status == 403 and "blocked" in str(info.value)
    with pytest.raises(TelegramError) as info:
        client.send(CHAT, "x")
    assert info.value.status == 502


# ---------------- vòng long polling ----------------
class FakeClient:
    def __init__(self, batches=(), send_errors=()):
        self.batches = list(batches)
        self.offsets = []
        self.sent = []
        self.send_errors = list(send_errors)

    def get_updates(self, offset, timeout):
        self.offsets.append(offset)
        b = self.batches.pop(0) if self.batches else []
        if isinstance(b, BaseException):
            raise b
        return b

    def send(self, chat_id, text):
        if self.send_errors:
            e = self.send_errors.pop(0)
            if e is not None:
                raise e
        self.sent.append((chat_id, text))


def test_offset_persisted_and_advanced_even_on_handler_error(conn, monkeypatch):
    real = bot.handle_update

    def flaky(update, conn_, cfg, now, **kw):
        if update["update_id"] == 10:
            raise RuntimeError("handler hỏng")
        return real(update, conn_, cfg, now, **kw)

    monkeypatch.setattr(bot, "handle_update", flaky)
    client = FakeClient([[upd("/fan on", uid=10), upd("/status", uid=11)], []])
    bot.poll_once(conn, client, CFG, NOW)
    assert get_state(conn, "telegram_offset") == "12"
    assert [c for c, _ in client.sent] == [CHAT]          # chỉ trả lời update 11
    bot.poll_once(conn, client, CFG, NOW)
    assert client.offsets == [None, 12]                   # không lặp lại tin lỗi


def test_poll_once_reply_send_failure_does_not_block(conn):
    client = FakeClient([[upd("/fan on", uid=3)]], send_errors=[TelegramError("mạng lỗi")])
    bot.poll_once(conn, client, CFG, NOW)
    assert get_state(conn, "telegram_offset") == "4"
    assert pending(conn)["fan"] is True


def test_poll_once_rolls_back_partial_writes_on_handler_error(conn, monkeypatch):
    def broken(update, conn_, cfg, now, **kw):
        conn_.execute("INSERT INTO events (ts, kind, detail) VALUES (1, 'x', 'y')")
        raise RuntimeError("hỏng giữa chừng")

    monkeypatch.setattr(bot, "handle_update", broken)
    bot.poll_once(conn, FakeClient([[upd("/fan on", uid=1)]]), CFG, NOW)
    assert conn.execute("SELECT count(*) FROM events WHERE kind = 'x'").fetchone()[0] == 0
    assert get_state(conn, "telegram_offset") == "2"


def test_poll_once_commits_command_and_offset_together(conn, tmp_path, monkeypatch):
    # Lỗi khi ghi offset: lệnh (pending_override + event) bị huỷ cùng, không có trạng thái
    # "đã áp dụng lệnh nhưng chưa ghi offset" (crash ở đó sẽ áp dụng lại lệnh).
    real_set_state = bot.set_state
    calls = {"n": 0}

    def failing_set_state(conn_, key, value, *, commit=True):
        if key == bot.OFFSET_KEY and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("đĩa đầy")
        return real_set_state(conn_, key, value, commit=commit)

    monkeypatch.setattr(bot, "set_state", failing_set_state)
    bot.poll_once(conn, FakeClient([[upd("/fan on", uid=20)]]), CFG, NOW)
    assert pending(conn) is None
    assert command_events(conn) == []
    assert get_state(conn, "telegram_offset") == "21"      # vẫn tiến offset


def test_handle_update_commit_false_leaves_transaction_open(conn, tmp_path):
    other = connect(tmp_path / "aiot.db")
    try:
        bot.handle_update(upd("/fan on", uid=1), conn, CFG, NOW, commit=False)
        assert get_state(other, "pending_override") is None   # chưa commit
        conn.commit()
        assert get_state(other, "pending_override") is not None
    finally:
        other.close()


def test_future_dated_command_rejected(conn):
    future = NOW + CFG.telegram.stale_command_s + 1
    for text in ("/fan on", "/mode auto", "/quiet 22 6"):
        reply = handle(conn, text, date=future)
        assert "bỏ" in reply.lower()
    assert pending(conn) is None
    assert get_state(conn, "quiet_hours") is None
    handle(conn, "/fan on", date=NOW + CFG.telegram.stale_command_s)   # đúng ngưỡng: áp dụng
    assert pending(conn)["fan"] is True


def test_control_refused_while_clock_unsynced(conn, monkeypatch):
    monkeypatch.setattr(bot, "is_clock_trusted", lambda: False)
    for text in ("/fan on", "/mode manual", "/quiet 22 6"):
        reply = handle(conn, text)
        assert "đồng bộ" in reply and "thử lại" in reply.lower()
    assert pending(conn) is None
    assert get_state(conn, "quiet_hours") is None
    assert all("not synced" in d for d in command_events(conn))
    for text in ("/now", "/status", "/help", "/forecast", "/report"):
        assert handle(conn, text)
    # injection trực tiếp cũng được tôn trọng
    reply = bot.handle_update(upd("/fan on"), conn, CFG, NOW, clock_synced=lambda: True)
    assert pending(conn)["fan"] is True and "bật" in reply


def test_poll_once_passes_clock_check(conn):
    client = FakeClient([[upd("/fan on", uid=5)]])
    bot.poll_once(conn, client, CFG, NOW, clock_synced=lambda: False)
    assert pending(conn) is None
    assert "đồng bộ" in client.sent[0][1]
    assert get_state(conn, "telegram_offset") == "6"


class FakeStop:
    """Event giả: ghi lại các lần chờ, tự dừng sau n lần."""

    def __init__(self, n):
        self.n = n
        self.waits = []

    def is_set(self):
        return len(self.waits) >= self.n

    def wait(self, t):
        self.waits.append(t)
        return self.is_set()


def test_polling_backoff_capped_at_60s(conn):
    client = FakeClient([TelegramError("mạng lỗi")] * 10)
    stop = FakeStop(10)
    bot.run_polling(conn, client, CFG, stop)
    assert stop.waits[:5] == [5, 10, 20, 40, 60]
    assert max(stop.waits) == 60 == bot.POLL_BACKOFF_MAX_S


def test_polling_honors_retry_after(conn):
    stop = FakeStop(1)
    bot.run_polling(conn, FakeClient([TelegramError("x", status=429, retry_after=9)]), CFG, stop)
    assert stop.waits == [9]


# ---------------- gửi outbox ----------------
def test_outbox_retries_after_network_failure_and_honors_429(conn):
    enqueue(conn, "fan_on", "Đã bật quạt", NOW - 10)
    client = FakeClient(send_errors=[TelegramError("mạng lỗi")])
    sender = bot.OutboxSender(client, [CHAT])
    wait = sender.deliver(conn, NOW)
    assert wait > 0
    assert len(pending_messages(conn)) == 1               # lỗi mạng: giữ tin

    client.send_errors = [TelegramError("chậm lại", status=429, retry_after=7)]
    wait = sender.deliver(conn, NOW)
    assert wait >= 7
    assert len(pending_messages(conn)) == 1

    assert sender.deliver(conn, NOW) == 0
    assert pending_messages(conn) == []
    assert client.sent == [(CHAT, "Đã bật quạt")]


def test_outbox_marks_sent_only_after_all_chats(conn):
    enqueue(conn, "fan_on", "A", NOW - 10)
    client = FakeClient(send_errors=[None, TelegramError("mạng lỗi")])
    sender = bot.OutboxSender(client, [111, 222])
    assert sender.deliver(conn, NOW) > 0
    assert len(pending_messages(conn)) == 1
    assert sender.deliver(conn, NOW) == 0
    assert pending_messages(conn) == []
    # chat đã nhận không bị gửi lại
    assert client.sent == [(111, "A"), (222, "A")]


def test_outbox_keeps_order_and_stops_at_first_failure(conn):
    enqueue(conn, "fan_on", "1", NOW - 30)
    enqueue(conn, "fan_off", "2", NOW - 20)
    client = FakeClient(send_errors=[TelegramError("mạng lỗi")])
    bot.OutboxSender(client, [CHAT]).deliver(conn, NOW)
    assert client.sent == []
    assert [r.text for r in pending_messages(conn)] == ["1", "2"]


def test_outbox_collapses_stale_alerts_before_sending(conn):
    for i in range(3):
        enqueue(conn, "aqi_change", f"cũ {i}", NOW - 7200 - i)
    client = FakeClient()
    bot.OutboxSender(client, [CHAT]).deliver(conn, NOW)
    assert len(client.sent) == 1 and "3 cảnh báo" in client.sent[0][1]


def test_outbox_permanent_chat_error_does_not_block_queue(conn):
    enqueue(conn, "fan_on", "A", NOW - 10)
    client = FakeClient(send_errors=[TelegramError("Forbidden", status=403), None])
    sender = bot.OutboxSender(client, [111, 222])
    assert sender.deliver(conn, NOW) == 0
    assert pending_messages(conn) == []
    assert client.sent == [(222, "A")]


def test_outbox_backoff_grows_and_resets(conn):
    enqueue(conn, "fan_on", "A", NOW)
    client = FakeClient(send_errors=[TelegramError("x"), TelegramError("x")])
    sender = bot.OutboxSender(client, [CHAT])
    w1 = sender.deliver(conn, NOW)
    w2 = sender.deliver(conn, NOW)
    assert w2 > w1
    assert sender.deliver(conn, NOW) == 0


# ---------------- khởi động ----------------
def test_main_refuses_without_token_or_allowlist(tmp_path, monkeypatch, caplog):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"db_path: {(tmp_path / 'a.db').as_posix()}\n", encoding="utf-8")
    env = tmp_path / ".env"
    for var in ("TELEGRAM_TOKEN", "ALLOWED_CHAT_IDS"):
        monkeypatch.delenv(var, raising=False)

    env.write_text("ALLOWED_CHAT_IDS=1\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        assert bot.main(["--config", str(cfg_path), "--env", str(env)]) != 0
    assert "TELEGRAM_TOKEN" in caplog.text

    caplog.clear()
    env.write_text(f"TELEGRAM_TOKEN={TOKEN}\nALLOWED_CHAT_IDS=abc\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        assert bot.main(["--config", str(cfg_path), "--env", str(env)]) != 0
    assert "ALLOWED_CHAT_IDS" in caplog.text
    assert TOKEN not in caplog.text


def test_main_exits_nonzero_when_worker_thread_dies(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"db_path: {(tmp_path / 'a.db').as_posix()}\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_TOKEN={TOKEN}\nALLOWED_CHAT_IDS={CHAT}\n", encoding="utf-8")
    for var in ("TELEGRAM_TOKEN", "ALLOWED_CHAT_IDS"):
        monkeypatch.delenv(var, raising=False)

    def broken_polling(conn, client, cfg, stop):
        raise RuntimeError("hỏng")

    monkeypatch.setattr(bot, "run_polling", broken_polling)
    monkeypatch.setattr(bot, "run_delivery", lambda conn, sender, stop: stop.wait(5))
    monkeypatch.setattr(bot.signal, "signal", lambda *a: None)
    assert bot.main(["--config", str(cfg_path), "--env", str(env)]) == 1


# ---------------- sửa lỗi review cuối nhánh ----------------
def test_pending_override_carries_command_time(conn):
    handle(conn, "/fan on", uid=1, now=NOW + 5)
    assert pending(conn)["at"] == NOW + 5 and isinstance(pending(conn)["at"], int)
    handle(conn, "/mode auto", uid=2, now=NOW + 6)
    assert pending(conn) == {"fan": None, "id": 2, "at": NOW + 6}
    handle(conn, "/mode manual", uid=3, now=NOW + 7)
    assert pending(conn)["at"] == NOW + 7


def test_status_warns_when_controller_heartbeat_missing_or_old(conn):
    loop = CFG.control.loop_s
    reply = handle(conn, "/status")
    assert "aiot-controller" in reply                       # chưa từng thấy controller chạy
    set_state(conn, "controller_heartbeat", NOW - 3 * loop - 1)
    assert "aiot-controller" in handle(conn, "/status")
    set_state(conn, "controller_heartbeat", NOW - 3 * loop)
    assert "aiot-controller" not in handle(conn, "/status")


def test_fan_and_mode_replies_warn_when_controller_stale(conn):
    set_state(conn, "controller_heartbeat", NOW - 3600)
    for text in ("/fan on", "/mode auto", "/mode manual"):
        assert "aiot-controller" in handle(conn, text)
    set_state(conn, "controller_heartbeat", NOW - 10)
    for text in ("/fan off", "/mode auto"):
        assert "aiot-controller" not in handle(conn, text)


def test_old_report_request_dropped_without_building_report(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "build_report", lambda *a, **k: calls.append(1) or "BÁO CÁO")
    old = NOW - CFG.telegram.stale_command_s - 1
    reply = handle(conn, "/report", date=old)
    assert calls == [] and "bỏ" in reply.lower() and "/report" in reply
    assert handle(conn, "/report", date=NOW - CFG.telegram.stale_command_s) == "BÁO CÁO"
    assert calls == [1]


# ---------------- ảnh: sendPhoto, outbox có ảnh, /photo ----------------
class MultipartSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, data=None, files=None, timeout=None):
        uploaded = None
        if files:
            name, f, mime = files["photo"]
            uploaded = (name, f.read(), mime)
        self.calls.append({"url": url, "json": json, "data": data, "files": uploaded,
                           "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def test_send_photo_multipart_plain_caption(tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"JPEG")
    session = MultipartSession([FakeResponse(payload={"ok": True, "result": {}})])
    TelegramClient(TOKEN, session=session).send_photo(CHAT, str(photo), "x" * 2000)
    call = session.calls[0]
    assert call["url"].endswith("/sendPhoto")
    assert call["files"] == ("a.jpg", b"JPEG", "image/jpeg")
    assert str(call["data"]["chat_id"]) == str(CHAT)
    assert len(call["data"]["caption"]) <= 1024
    assert "parse_mode" not in call["data"]


def test_send_photo_errors_hide_token(tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"JPEG")
    err = requests.exceptions.ConnectionError(f"lỗi /bot{TOKEN}/sendPhoto")
    session = MultipartSession([err, FakeResponse(429, {"ok": False, "error_code": 429,
                                                        "parameters": {"retry_after": 3}})])
    client = TelegramClient(TOKEN, session=session)
    with pytest.raises(TelegramError) as info:
        client.send_photo(CHAT, str(photo), "c")
    assert TOKEN not in str(info.value) and info.value.network
    with pytest.raises(TelegramError) as info:
        client.send_photo(CHAT, str(photo), "c")
    assert info.value.retry_after == 3


def test_set_my_commands_payload():
    session = FakeSession([FakeResponse(payload={"ok": True, "result": True})])
    TelegramClient(TOKEN, session=session).set_my_commands(bot.BOT_COMMANDS)
    call = session.calls[0]
    assert call["url"].endswith("/setMyCommands")
    commands = call["json"]["commands"]
    assert [c["command"] for c in commands] == [
        "now", "status", "forecast", "report", "photo", "fan", "mode", "quiet", "help"]
    for c in commands:
        assert set(c) == {"command", "description"}
        assert 1 <= len(c["description"]) <= 256 and not c["command"].startswith("/")


class PhotoClient(FakeClient):
    def __init__(self, *a, photo_errors=(), commands_error=None, **kw):
        super().__init__(*a, **kw)
        self.photos = []
        self.photo_errors = list(photo_errors)
        self.commands = None
        self.commands_error = commands_error

    def send_photo(self, chat_id, path, caption):
        if self.photo_errors:
            e = self.photo_errors.pop(0)
            if e is not None:
                raise e
        self.photos.append((chat_id, path, caption))

    def set_my_commands(self, commands):
        if self.commands_error is not None:
            raise self.commands_error
        self.commands = commands


def test_register_commands_failure_only_warns(caplog):
    client = PhotoClient()
    bot.register_commands(client)
    assert client.commands == bot.BOT_COMMANDS
    for error in (TelegramError("mạng lỗi"), RuntimeError("lạ")):
        with caplog.at_level(logging.WARNING, logger="aiot.bot"):
            bot.register_commands(PhotoClient(commands_error=error))   # không raise
    assert "setMyCommands" in caplog.text


def test_outbox_row_with_photo_uses_send_photo(conn, tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"JPEG")
    enqueue(conn, "weather_sunny", "Trời nắng", NOW - 10, photo_path=str(photo))
    client = PhotoClient()
    assert bot.OutboxSender(client, [111, 222]).deliver(conn, NOW) == 0
    assert client.photos == [(111, str(photo), "Trời nắng"), (222, str(photo), "Trời nắng")]
    assert client.sent == [] and pending_messages(conn) == []


def test_outbox_missing_photo_falls_back_to_text(conn, tmp_path):
    enqueue(conn, "weather_rain", "Có thể mưa", NOW - 10, photo_path=str(tmp_path / "mat.jpg"))
    client = PhotoClient()
    assert bot.OutboxSender(client, [CHAT]).deliver(conn, NOW) == 0
    assert client.photos == [] and client.sent == [(CHAT, "Có thể mưa")]


def test_outbox_photo_deleted_during_send_falls_back_to_text(conn, tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"JPEG")
    enqueue(conn, "weather_rain", "Có thể mưa", NOW - 10, photo_path=str(photo))
    client = PhotoClient(photo_errors=[FileNotFoundError("đã xoá")])
    assert bot.OutboxSender(client, [CHAT]).deliver(conn, NOW) == 0
    assert client.sent == [(CHAT, "Có thể mưa")]


def test_outbox_photo_network_error_keeps_message(conn, tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"JPEG")
    enqueue(conn, "weather_sunny", "Nắng", NOW - 10, photo_path=str(photo))
    client = PhotoClient(photo_errors=[TelegramError("mạng lỗi")])
    sender = bot.OutboxSender(client, [CHAT])
    assert sender.deliver(conn, NOW) > 0
    assert len(pending_messages(conn)) == 1 and client.sent == []
    assert sender.deliver(conn, NOW) == 0
    assert client.photos == [(CHAT, str(photo), "Nắng")]


def _cfg_photos(tmp_path):
    return dataclasses.replace(
        CFG, weather=dataclasses.replace(CFG.weather, photo_dir=str(tmp_path / "photos"),
                                         camera_index=2))


def test_photo_command_captures_and_returns_photo(conn, tmp_path, monkeypatch):
    calls = []

    def fake_capture(path, index=0, **kw):
        calls.append((path, index))
        return True
    monkeypatch.setattr(bot, "capture_jpeg", fake_capture)
    cfg = _cfg_photos(tmp_path)
    reply = bot.handle_update(upd("/photo"), conn, cfg, NOW)
    assert isinstance(reply, bot.PhotoReply)
    assert reply.path == calls[0][0] and calls[0][1] == 2
    assert reply.path.startswith(str(tmp_path / "photos"))
    assert "14:00" in reply.caption
    assert pending_messages(conn) == []                       # gửi trực tiếp, không qua outbox


def test_photo_command_capture_failure_replies_text(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "capture_jpeg", lambda path, index=0, **kw: False)
    reply = bot.handle_update(upd("/photo"), conn, _cfg_photos(tmp_path), NOW)
    assert isinstance(reply, str) and "Không chụp được ảnh" in reply


def test_stale_photo_command_does_not_capture(conn, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "capture_jpeg", lambda *a, **k: calls.append(a) or True)
    reply = bot.handle_update(upd("/photo", date=NOW - 3600), conn, _cfg_photos(tmp_path), NOW)
    assert calls == [] and isinstance(reply, str) and "/photo" in reply


def test_photo_works_while_clock_unsynced(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "capture_jpeg", lambda path, index=0, **kw: True)
    reply = bot.handle_update(upd("/photo"), conn, _cfg_photos(tmp_path), NOW,
                              clock_synced=lambda: False)
    assert isinstance(reply, bot.PhotoReply)


def test_poll_once_sends_photo_reply(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "capture_jpeg", lambda path, index=0, **kw: True)
    client = PhotoClient([[upd("/photo", uid=7)]])
    bot.poll_once(conn, client, _cfg_photos(tmp_path), NOW)
    assert len(client.photos) == 1 and client.photos[0][0] == CHAT
    assert client.sent == []


def test_poll_once_photo_send_failure_falls_back_or_logs(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "capture_jpeg", lambda path, index=0, **kw: True)
    client = PhotoClient([[upd("/photo", uid=8)]], photo_errors=[FileNotFoundError("mất")])
    bot.poll_once(conn, client, _cfg_photos(tmp_path), NOW)
    assert len(client.sent) == 1 and "Không gửi được ảnh" in client.sent[0][1]
    client = PhotoClient([[upd("/photo", uid=9)]], photo_errors=[TelegramError("mạng")])
    bot.poll_once(conn, client, _cfg_photos(tmp_path), NOW)   # không raise
    assert get_state(conn, "telegram_offset") == "10"


def test_help_lists_photo(conn):
    assert "/photo" in handle(conn, "/help")
