"""
Dịch vụ bot Telegram: long polling nhận lệnh, gửi tin trong outbox (gửi bù khi có mạng lại).

- Chỉ phản hồi chat trong ALLOWED_CHAT_IDS. Chat khác: không trả lời, không ghi DB
  (chỉ một dòng log debug, không có nội dung tin).
- Bot không bao giờ chạm GPIO. Lệnh điều khiển chỉ ghi khoá state 'pending_override'
  (JSON) cho controller: {"fan": true|false, "ttl_s": int, "id": update_id, "at": now}
  đặt ghi đè, {"fan": null, "id": update_id, "at": now} xoá ghi đè. "id" làm hai lệnh
  giống nhau liên tiếp thành hai văn bản khác nhau (controller xoá khoá theo đúng văn bản
  đã đọc); "at" (giây epoch) để controller bỏ lệnh đã chờ quá lâu (controller từng dừng).
- /status và trả lời /fan, /mode cảnh báo khi khoá 'controller_heartbeat' (controller ghi
  mỗi vòng) thiếu hoặc cũ hơn CONTROLLER_STALE_LOOPS vòng loop_s.
- /quiet ghi khoá 'quiet_hours' ("start,end") mà notifier dùng.
- Mọi lệnh điều khiển (/fan, /mode, /quiet) được ghi vào events (kind 'command',
  detail = văn bản lệnh + chat id), cùng transaction với lệnh ghi state.
- Lệnh điều khiển lệch quá telegram.stale_command_s giây so với đồng hồ Pi (cũ hoặc "ở
  tương lai") bị bỏ. Pi không có RTC, nên khi đồng hồ chưa tin được (chưa đồng bộ NTP lần
  nào từ lúc khởi động, xem aiot.collector.is_clock_trusted) thì lệnh điều khiển bị từ chối
  hẳn. Lệnh chỉ đọc luôn được trả lời, trừ /report và /photo cũ hơn stale_command_s (tồn
  đọng 24 giờ khi bot chạy lần đầu hay sau khi mất mạng): bỏ để không gọi Gemini hay chụp
  ảnh cho từng tin cũ.
- /photo chụp ảnh camera USB ngay và trả lời bằng ảnh, không qua outbox. Chụp trong tiến
  trình con có timeout 20 s (aiot.camera.capture_subprocess), để camera treo không làm
  đứng luồng long polling (mọi lệnh, kể cả /fan). Chụp lỗi/quá giờ (camera bận vì
  pi_camera_client.py, không có camera/cv2) thì trả lời chữ.
- Tin outbox có photo_path (cảnh báo nắng/mưa của aiot.weather) được gửi bằng sendPhoto,
  chữ làm chú thích; file ảnh không còn thì gửi chữ.
- Luồng long polling đặt menu lệnh (setMyCommands) khi bắt đầu; lỗi chỉ ghi cảnh báo và
  thử lại sau lần getUpdates thành công kế tiếp (tối đa REGISTER_ATTEMPTS lần).
- Offset getUpdates lưu ở khoá 'telegram_offset', commit cùng transaction với thay đổi
  của lệnh (crash giữa chừng không áp dụng lại lệnh), và luôn tăng kể cả khi xử lý một
  update bị lỗi, để một tin hỏng không làm bot lặp lại mãi.
- Lỗi mạng khi long polling: chờ lâu dần nhưng tối đa POLL_BACKOFF_MAX_S (60 s).
- Hai luồng trong cùng tiến trình, mỗi luồng một kết nối SQLite và một TelegramClient:
  long polling (trả lời lệnh) và gửi outbox (mỗi DELIVERY_INTERVAL_S giây; lỗi mạng thì
  giữ tin và chờ lâu dần; HTTP 429 thì chờ retry_after).
- Token không bao giờ xuất hiện trong log: TelegramError đã che token, log có bộ lọc
  che token, urllib3 bị đặt mức WARNING (không ghi URL request).
- Thiếu TELEGRAM_TOKEN hoặc ALLOWED_CHAT_IDS rỗng: ghi lỗi rõ ràng và thoát mã 2.

Chạy: cd ~/BTL_IOT && venv/bin/python -m aiot.bot
"""
import argparse
import json
import logging
import re
import signal
import threading
import os
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from aiot.aqi import classify
from aiot.camera import capture_subprocess as capture_photo
from aiot.control import ControlState, fan_on, override_active
from aiot.collector import is_clock_trusted
from aiot.db import connect, get_state, init_schema, log_event, set_state
from aiot.forecast import MODEL_NAMES, latest_rows
from aiot.notifier import collapse_stale_alerts, mark_sent, pending_messages, quiet_window
from aiot.report import build_report
from aiot.telegram_api import TelegramClient, TelegramError, install_log_redaction

log = logging.getLogger("aiot.bot")

OFFSET_KEY = "telegram_offset"
QUIET_KEY = "quiet_hours"
PENDING_KEY = "pending_override"       # cùng khoá với aiot.controller (không import để
STATE_KEY = "control_state"            # bot không kéo theo module điều khiển GPIO)
HEARTBEAT_KEY = "controller_heartbeat"
CONTROLLER_STALE_LOOPS = 3             # heartbeat cũ hơn 3 vòng loop_s: controller không chạy
NOW_MAX_AGE_S = 300                    # /now: mẫu cũ hơn 5 phút coi như chưa có dữ liệu
DELIVERY_INTERVAL_S = 5
BACKOFF_MIN_S = 5
BACKOFF_MAX_S = 300
POLL_BACKOFF_MAX_S = 60                # lệnh gửi ngay khi có mạng lại không bị chờ tới quá cũ
REGISTER_ATTEMPTS = 3                  # số lần thử setMyCommands mỗi lần bot chạy
PERMANENT_STATUSES = frozenset({400, 403})   # chat không tồn tại / chặn bot: bỏ chat đó

HELP = ("Các lệnh:\n"
        "/now - số liệu cảm biến mới nhất\n"
        "/status - chế độ, quạt, ghi đè, cảm biến, giờ yên lặng\n"
        "/forecast - dự báo PM2.5 6 giờ tới\n"
        "/report - báo cáo 24 giờ qua\n"
        "/photo - chụp ảnh ban công ngay\n"
        "/fan on | /fan off - bật/tắt quạt thủ công (có thời hạn)\n"
        "/mode auto - về tự động theo PM2.5\n"
        "/mode manual - giữ quạt như hiện tại (có thời hạn)\n"
        "/quiet 22 6 - đặt giờ yên lặng từ 22h tới 6h")
# Menu lệnh hiện khi gõ "/" trong Telegram (setMyCommands); mô tả tối đa 256 ký tự
BOT_COMMANDS = [
    {"command": "now", "description": "Số liệu cảm biến mới nhất"},
    {"command": "status", "description": "Chế độ, quạt, ghi đè, cảm biến, giờ yên lặng"},
    {"command": "forecast", "description": "Dự báo PM2.5 6 giờ tới"},
    {"command": "report", "description": "Báo cáo không khí 24 giờ qua"},
    {"command": "photo", "description": "Chụp ảnh ban công ngay"},
    {"command": "fan", "description": "Bật/tắt quạt thủ công: /fan on hoặc /fan off"},
    {"command": "mode", "description": "Chế độ quạt: /mode auto hoặc /mode manual"},
    {"command": "quiet", "description": "Đặt giờ yên lặng, ví dụ /quiet 22 6"},
    {"command": "help", "description": "Danh sách lệnh"},
]
USAGE_FAN = "Cách dùng: /fan on hoặc /fan off"
USAGE_MODE = "Cách dùng: /mode auto hoặc /mode manual"
USAGE_QUIET = ("Cách dùng: /quiet <giờ bắt đầu> <giờ kết thúc>, mỗi giờ từ 0 tới 23, "
               "ví dụ /quiet 22 6. Gửi /quiet 0 0 để tắt giờ yên lặng.")
_HOUR_RE = re.compile(r"[0-9]{1,2}", re.ASCII)


# ---------------- định dạng ----------------
def _fmt_time(ts, seconds=False):
    return datetime.fromtimestamp(int(ts)).strftime("%H:%M:%S %d/%m" if seconds else "%H:%M %d/%m")


def _fmt_age(seconds):
    """1200 -> '20 phút', 5400 -> '1 giờ 30 phút'."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} giây"
    if s < 3600:
        return f"{s // 60} phút"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} giờ" + (f" {m} phút" if m else "")
    return f"{s // 86400} ngày"


def _num(value, digits=1, unit=""):
    if value is None:
        return "không đọc được"
    text = f"{value:.{digits}f}".replace(".", ",")
    return f"{text} {unit}".rstrip()


def _on_off(on):
    return "bật" if on else "tắt"


# ---------------- đọc DB ----------------
def _latest_sample(conn):
    return conn.execute(
        "SELECT ts, pm1, pm25, pm10, temp, hum, lux, mq135_do FROM raw_readings "
        "ORDER BY ts DESC LIMIT 1").fetchone()


def _control_state(conn):
    """(ControlState, lỗi). Chưa có trạng thái: mặc định BÌNH THƯỜNG."""
    text = get_state(conn, STATE_KEY)
    if text is None:
        return ControlState(), None
    try:
        return ControlState.from_json(text), None
    except ValueError as e:
        return None, str(e)


def _controller_warning(conn, cfg, now):
    """Câu cảnh báo khi controller không chạy (heartbeat thiếu/cũ), ngược lại None."""
    text = get_state(conn, HEARTBEAT_KEY)
    try:
        beat = int(text) if text is not None else None
    except ValueError:
        beat = None
    if beat is not None and now - beat <= CONTROLLER_STALE_LOOPS * cfg.control.loop_s:
        return None
    since = (f"từ {_fmt_time(beat)} (cách đây {_fmt_age(now - beat)})" if beat is not None
             else "(chưa thấy lần chạy nào)")
    return (f"Cảnh báo: bộ điều khiển quạt (dịch vụ aiot-controller) không chạy {since}; "
            f"lệnh quạt chưa được áp dụng cho tới khi dịch vụ chạy lại.")


def _with_controller_warning(conn, cfg, now, reply):
    warning = _controller_warning(conn, cfg, now)
    return reply if warning is None else f"{reply}\n{warning}"


# ---------------- lệnh chỉ đọc ----------------
def cmd_now(conn, cfg, now, args):
    row = _latest_sample(conn)
    if row is None:
        return "Chưa có dữ liệu cảm biến."
    ts, pm1, pm25, pm10, temp, hum, lux, mq = row
    age = now - ts
    if age > NOW_MAX_AGE_S:
        return (f"Chưa có dữ liệu mới: mẫu gần nhất cách đây {_fmt_age(age)} "
                f"(lúc {_fmt_time(ts)}). Kiểm tra dịch vụ collector và cảm biến.")
    pm25_text = _num(pm25, unit="µg/m³")
    if pm25 is not None:
        try:
            pm25_text += f" ({classify(pm25).label})"
        except ValueError:
            pass
    if mq is None:
        mq_text = "chưa có (đang sấy hoặc lỗi đọc)"
    elif mq == 0:
        mq_text = "vượt ngưỡng (đèn DO sáng)"
    else:
        mq_text = "chưa vượt ngưỡng"
    return (f"Số liệu lúc {_fmt_time(ts, seconds=True)} (cách đây {_fmt_age(age)}):\n"
            f"- PM2.5: {pm25_text}\n"
            f"- PM1.0: {_num(pm1, unit='µg/m³')}\n"
            f"- PM10: {_num(pm10, unit='µg/m³')}\n"
            f"- Nhiệt độ: {_num(temp, unit='°C')}\n"
            f"- Độ ẩm: {_num(hum, 0, '%')}\n"
            f"- Ánh sáng: {_num(lux, 0, 'lux')}\n"
            f"- MQ135 (chỉ báo khí, không phải nồng độ): {mq_text}")


def cmd_status(conn, cfg, now, args):
    lines = ["Trạng thái trạm:"]
    st, error = _control_state(conn)
    if st is None:
        lines.append(f"- Trạng thái điều khiển: không đọc được ({error})")
    else:
        lines.append(f"- Chế độ luật tự động: {'BẢO VỆ' if st.mode == 'PROTECT' else 'BÌNH THƯỜNG'}")
        lines.append(f"- Quạt: đang {_on_off(fan_on(st, now))}")
        if override_active(st, now):
            lines.append(f"- Ghi đè thủ công: giữ quạt {_on_off(st.override_fan)} tới "
                         f"{_fmt_time(st.override_until)} (gửi /mode auto để về tự động)")
        else:
            lines.append("- Ghi đè thủ công: không")
        lines.append("- Cảm biến PM2.5: " + ("đang lỗi, mất dữ liệu (quạt giữ nguyên trạng thái)"
                                            if st.fault_active else "bình thường"))
    if get_state(conn, PENDING_KEY) is not None:
        lines.append("- Có lệnh ghi đè đang chờ bộ điều khiển áp dụng")
    row = _latest_sample(conn)
    if row is None:
        lines.append("- Dữ liệu: chưa có dữ liệu")
    else:
        age = now - row[0]
        note = " (cũ, chưa có dữ liệu mới)" if age > NOW_MAX_AGE_S else ""
        lines.append(f"- Mẫu cảm biến mới nhất: cách đây {_fmt_age(age)}{note}")
    start, end = quiet_window(conn, cfg)
    lines.append(f"- Giờ yên lặng: từ {start}h tới {end}h" if start != end
                 else "- Giờ yên lặng: không có")
    return _with_controller_warning(conn, cfg, now, "\n".join(lines))


def cmd_forecast(conn, cfg, now, args):
    max_age = cfg.control.forecast_max_age_s
    found = latest_rows(conn, now, max_age)
    if found is None or not found[2]:
        return (f"Chưa có dự báo (hoặc dự báo gần nhất cũ hơn {_fmt_age(max_age)}). "
                f"Dự báo được lập mỗi giờ khi có đủ dữ liệu PM2.5 của 3 giờ gần đây.")
    made_at, model, rows = found
    name = MODEL_NAMES.get(model)
    model_text = f"{model} ({name})" if name else model
    lines = [f"Dự báo PM2.5 trung bình từng giờ (lập lúc {_fmt_time(made_at)}, "
             f"cách đây {_fmt_age(now - made_at)}):"]
    for horizon, value in rows:
        start = made_at + (horizon - 1) * 3600
        try:
            label = f" ({classify(value).label})"
        except ValueError:
            label = ""
        lines.append(f"- {datetime.fromtimestamp(start).strftime('%H:00')}: "
                     f"{_num(value, unit='µg/m³')}{label}")
    lines.append(f"Mô hình: {model_text}")
    return "\n".join(lines)


def cmd_report(conn, cfg, now, args):
    """Báo cáo 24 giờ qua, trả lời trực tiếp (không qua outbox, không đụng khoá báo cáo
    sáng). Có thể gọi Gemini (tối đa gemini.timeout_s giây, tính vào giới hạn mỗi ngày)."""
    try:
        return build_report(conn, now, cfg)
    except Exception:
        log.exception("Lỗi khi lập báo cáo")
        return "Không lập được báo cáo lúc này. Gửi /now để xem số liệu hiện tại, thử lại sau."


PHOTO_FAILED = "Camera không phản hồi, thử lại sau."


class PhotoReply(NamedTuple):
    """Trả lời bằng ảnh (poll_once gửi bằng sendPhoto)."""
    path: str
    caption: str


def cmd_photo(conn, cfg, now, args):
    """Chụp ảnh camera USB ngay. Ảnh lưu trong weather.photo_dir (aiot.weather xoá ảnh cũ)."""
    w = cfg.weather
    path = os.path.join(w.photo_dir, f"{datetime.fromtimestamp(now):%Y%m%d_%H%M%S}_photo.jpg")
    if not capture_photo(path, index=w.camera_index):
        return PHOTO_FAILED
    return PhotoReply(path, f"Ảnh ban công lúc {_fmt_time(now, seconds=True)}.")


def cmd_help(conn, cfg, now, args):
    return HELP


# ---------------- lệnh điều khiển ----------------
def _apply_control(conn, now, ctx, writes):
    """Ghi các khoá state và sự kiện 'command' trong MỘT transaction.
    ctx["commit"] False: người gọi (poll_once) commit cùng offset."""
    try:
        for key, value in writes:
            set_state(conn, key, value, commit=False)
        log_event(conn, now, "command", f"{ctx['text'].strip()[:200]} chat={ctx['chat_id']}",
                  commit=False)
        if ctx["commit"]:
            conn.commit()
    except BaseException:
        if ctx["commit"]:
            conn.rollback()
        raise


def _pending_json(fan, update_id, now, ttl_s=None):
    data = {"fan": fan}
    if fan is not None:
        data["ttl_s"] = int(ttl_s)
    data["id"] = update_id
    data["at"] = int(now)
    return json.dumps(data, sort_keys=True)


def cmd_fan(conn, cfg, now, args, ctx):
    if len(args) != 1 or args[0] not in ("on", "off"):
        return USAGE_FAN
    on = args[0] == "on"
    ttl = cfg.control.override_ttl_s
    _apply_control(conn, now, ctx,
                   [(PENDING_KEY, _pending_json(on, ctx["update_id"], now, ttl))])
    return _with_controller_warning(conn, cfg, now, (
        f"Đã gửi lệnh {_on_off(on)} quạt thủ công. Bộ điều khiển áp dụng trong khoảng "
        f"{cfg.control.loop_s} giây; ghi đè có hiệu lực {_fmt_age(ttl)} "
        f"(tới {_fmt_time(now + ttl)}). Gửi /mode auto để về tự động."))


def cmd_mode(conn, cfg, now, args, ctx):
    if len(args) != 1 or args[0] not in ("auto", "manual"):
        return USAGE_MODE
    if args[0] == "auto":
        _apply_control(conn, now, ctx,
                       [(PENDING_KEY, _pending_json(None, ctx["update_id"], now))])
        return _with_controller_warning(conn, cfg, now, (
            f"Đã gửi lệnh về chế độ tự động: quạt theo luật PM2.5 trong khoảng "
            f"{cfg.control.loop_s} giây."))
    st, _ = _control_state(conn)
    on = fan_on(st, now) if st is not None else False
    ttl = cfg.control.override_ttl_s
    _apply_control(conn, now, ctx,
                   [(PENDING_KEY, _pending_json(on, ctx["update_id"], now, ttl))])
    return _with_controller_warning(conn, cfg, now, (
        f"Đã chuyển sang thủ công: giữ quạt đang {_on_off(on)} trong {_fmt_age(ttl)} "
        f"(tới {_fmt_time(now + ttl)}). Gửi /fan on|off để đổi, /mode auto để về tự động."))


def cmd_quiet(conn, cfg, now, args, ctx):
    if len(args) != 2 or not all(_HOUR_RE.fullmatch(a) for a in args):
        return USAGE_QUIET
    start, end = int(args[0]), int(args[1])
    if not (0 <= start < 24 and 0 <= end < 24):
        return USAGE_QUIET
    _apply_control(conn, now, ctx, [(QUIET_KEY, f"{start},{end}")])
    if start == end:
        return "Đã tắt giờ yên lặng: cảnh báo được gửi mọi lúc."
    return (f"Đã đặt giờ yên lặng từ {start}h tới {end}h. Trong giờ này chỉ cảnh báo "
            f"khẩn cấp được gửi ngay, các tin khác chờ hết giờ yên lặng.")


READ_ONLY = {
    "/now": cmd_now,
    "/status": cmd_status,
    "/forecast": cmd_forecast,
    "/report": cmd_report,
    "/photo": cmd_photo,
    "/help": cmd_help,
    "/start": cmd_help,
}
STALE_READ_ONLY = frozenset({"/report", "/photo"})   # lệnh chỉ đọc "đắt": bỏ khi quá cũ
CONTROL = {
    "/fan": cmd_fan,
    "/mode": cmd_mode,
    "/quiet": cmd_quiet,
}


def handle_update(update, conn, cfg, now, *, clock_synced=None, commit=True):
    """Xử lý một update của Telegram. Trả về văn bản trả lời, PhotoReply (/photo), hoặc None
    nếu không trả lời (chat không được phép, không phải tin chữ).

    clock_synced: hàm () -> bool, mặc định aiot.collector.is_clock_trusted. Pi không có
    RTC: khi đồng hồ chưa tin được thì không so được tuổi lệnh, nên lệnh điều khiển
    bị từ chối. commit False: không commit (poll_once commit cùng offset)."""
    now = int(now)
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if not isinstance(chat_id, int) or isinstance(chat_id, bool)             or chat_id not in cfg.secrets.allowed_chat_ids:
        log.debug("Bỏ qua update %s từ chat không được phép", update.get("update_id"))
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    parts = text.split()
    command = parts[0].lower().split("@", 1)[0]     # /fan@TenBot -> /fan
    args = [p.lower() for p in parts[1:]]

    if command in CONTROL:
        detail = f"{text.strip()[:200]} chat={chat_id}"
        synced = (clock_synced if clock_synced is not None else is_clock_trusted)()
        if not synced:
            log.info("Bỏ lệnh %s từ chat %s: đồng hồ Pi chưa đồng bộ NTP", command, chat_id)
            log_event(conn, now, "command", f"{detail} dropped: clock not synced", commit=commit)
            return (f"Chưa thực hiện lệnh {command}: đồng hồ của Pi chưa đồng bộ (vừa khởi động "
                    f"lại hoặc chưa có mạng), nên không kiểm tra được lệnh có cũ không. "
                    f"Thử lại sau vài phút.")
        stale_s = cfg.telegram.stale_command_s
        date = message.get("date")
        # abs(): lệnh "ở tương lai" cũng bị bỏ (đồng hồ lệch), không coi là mới
        if not isinstance(date, int) or isinstance(date, bool) or abs(now - date) > stale_s:
            if not isinstance(date, int) or isinstance(date, bool):
                age = "không rõ giờ"
            elif date > now:
                age = f"với giờ lệch {_fmt_age(date - now)} so với đồng hồ Pi"
            else:
                age = f"cách đây {_fmt_age(now - date)}"
            log.info("Bỏ lệnh cũ %s từ chat %s (%s)", command, chat_id, age)
            log_event(conn, now, "command", f"{detail} dropped: stale", commit=commit)
            return (f"Đã bỏ lệnh {command}: lệnh gửi {age}, quá {stale_s} giây "
                    f"(có thể do mất mạng). Gửi lại nếu vẫn cần.")
        ctx = {"chat_id": chat_id, "text": text, "update_id": update.get("update_id"),
               "commit": commit}
        log.info("Lệnh %s từ chat %s", text.strip()[:100], chat_id)
        return CONTROL[command](conn, cfg, now, args, ctx)

    if command in STALE_READ_ONLY:
        date = message.get("date")
        stale_s = cfg.telegram.stale_command_s
        if isinstance(date, int) and not isinstance(date, bool) and now - date > stale_s:
            # Tồn đọng sau khi bot dừng/mất mạng: mỗi /report cũ gọi Gemini một lần,
            # mỗi /photo cũ chụp một ảnh
            log.info("Bỏ %s cũ từ chat %s (cách đây %s giây)", command, chat_id, now - date)
            return (f"Đã bỏ lệnh {command} gửi cách đây {_fmt_age(now - date)} (quá {stale_s} "
                    f"giây, có thể do bot dừng hoặc mất mạng). Gửi lại {command} nếu vẫn cần.")
    handler = READ_ONLY.get(command)
    if handler is not None:
        return handler(conn, cfg, now, args)
    return "Không hiểu lệnh này.\n" + HELP


# ---------------- long polling ----------------
def _load_offset(conn):
    text = get_state(conn, OFFSET_KEY)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        log.warning("Khoá %s sai định dạng, lấy lại từ đầu", OFFSET_KEY)
        return None


def poll_once(conn, client, cfg, now=None, *, clock_synced=None):
    """Một lần getUpdates rồi xử lý từng update. now None: dùng giờ hiện tại sau khi nhận.

    Mỗi update: thay đổi của lệnh, sự kiện 'command' và offset mới được commit MỘT lần,
    nên crash giữa chừng không làm lệnh bị áp dụng lại. Xử lý lỗi thì rollback rồi vẫn
    ghi offset, để một tin hỏng không làm bot lặp lại mãi. Trả lời gửi lỗi chỉ ghi log."""
    offset = _load_offset(conn)
    updates = client.get_updates(offset, cfg.telegram.poll_timeout_s)
    for update in updates:
        if not isinstance(update, dict):
            continue
        update_id = update.get("update_id")
        new_offset = None
        if isinstance(update_id, int) and not isinstance(update_id, bool)                 and (offset is None or update_id + 1 > offset):
            new_offset = update_id + 1
        t = int(time.time()) if now is None else int(now)
        reply = None
        try:
            reply = handle_update(update, conn, cfg, t, clock_synced=clock_synced, commit=False)
            if new_offset is not None:
                set_state(conn, OFFSET_KEY, new_offset, commit=False)
            conn.commit()
        except Exception:
            log.exception("Lỗi khi xử lý update %s, bỏ qua", update_id)
            conn.rollback()
            reply = None
            if new_offset is not None:
                set_state(conn, OFFSET_KEY, new_offset)
        if new_offset is not None:
            offset = new_offset
        if reply:
            _send_reply(client, update["message"]["chat"]["id"], reply)


def _send_reply(client, chat_id, reply):
    """Gửi trả lời (chữ hoặc PhotoReply). Lỗi chỉ ghi log."""
    try:
        if isinstance(reply, PhotoReply):
            try:
                client.send_photo(chat_id, reply.path, reply.caption)
            except OSError as e:
                log.warning("Không mở được ảnh vừa chụp: %s", e)
                client.send(chat_id, "Không gửi được ảnh vừa chụp (file ảnh bị mất). "
                                     "Thử lại /photo.")
        else:
            client.send(chat_id, reply)
    except TelegramError as e:
        log.warning("Không gửi được trả lời tới chat %s: %s", chat_id, e)


def register_commands(client):
    """Đặt menu lệnh của bot (setMyCommands). Trả về True nếu được; lỗi chỉ ghi cảnh báo."""
    try:
        client.set_my_commands(BOT_COMMANDS)
        log.info("Đã đặt menu lệnh (%d lệnh)", len(BOT_COMMANDS))
        return True
    except Exception as e:
        log.warning("setMyCommands lỗi, bỏ qua (menu lệnh có thể chưa cập nhật): %s", e)
        return False


def _next_backoff(current, maximum=BACKOFF_MAX_S):
    return min(max(current * 2, BACKOFF_MIN_S), maximum)


def run_polling(conn, client, cfg, stop):
    registered = register_commands(client)
    attempts = 1
    backoff = 0
    while not stop.is_set():
        try:
            poll_once(conn, client, cfg)
            backoff = 0
            if not registered and attempts < REGISTER_ATTEMPTS:
                attempts += 1
                registered = register_commands(client)
        except TelegramError as e:
            if e.retry_after:
                wait = e.retry_after
            else:
                backoff = _next_backoff(backoff, POLL_BACKOFF_MAX_S)
                wait = backoff
            log.warning("getUpdates lỗi: %s; thử lại sau %s giây", e, wait)
            stop.wait(wait)
        except Exception:
            log.exception("Lỗi trong vòng long polling")
            try:
                conn.rollback()
            except Exception as e:
                log.error("Rollback lỗi: %s", e)
            stop.wait(10)


# ---------------- gửi outbox ----------------
class OutboxSender:
    """Gửi tin chưa gửi trong outbox tới mọi chat được phép, cũ trước.

    Một tin chỉ được mark_sent khi đã gửi tới mọi chat; chat đã nhận được nhớ trong bộ nhớ
    để lần thử lại không gửi trùng. Chat lỗi vĩnh viễn (HTTP 400/403) bị bỏ qua cho tin đó,
    để một chat hỏng không chặn cả hàng đợi.
    """

    def __init__(self, client, chat_ids):
        self.client = client
        self.chat_ids = sorted(chat_ids)
        self._delivered = {}            # outbox id -> set(chat id đã nhận)
        self._backoff = 0

    def deliver(self, conn, now):
        """Gửi hết những gì gửi được. Trả về số giây nên chờ trước lần sau (0: đã gửi hết)."""
        now = int(now)
        collapse_stale_alerts(conn, now)
        for row in pending_messages(conn):
            done = self._delivered.setdefault(row.id, set())
            for chat_id in self.chat_ids:
                if chat_id in done:
                    continue
                try:
                    self._send_row(chat_id, row)
                except TelegramError as e:
                    if e.retry_after:
                        log.warning("Telegram yêu cầu chờ %s giây (HTTP 429)", e.retry_after)
                        return float(max(1, e.retry_after))
                    if e.status in PERMANENT_STATUSES:
                        log.error("Bỏ chat %s cho tin %s (lỗi vĩnh viễn): %s", chat_id, row.id, e)
                        done.add(chat_id)
                        continue
                    self._backoff = _next_backoff(self._backoff)
                    log.warning("Chưa gửi được tin %s (%s): %s; thử lại sau %s giây",
                                row.id, row.kind, e, self._backoff)
                    return float(self._backoff)
                done.add(chat_id)
            mark_sent(conn, row.id, now)
            self._delivered.pop(row.id, None)
            log.info("Đã gửi tin %s (%s)", row.id, row.kind)
        self._backoff = 0
        return 0

    def _send_row(self, chat_id, row):
        """Tin có ảnh còn trên đĩa: sendPhoto (chữ làm chú thích); không thì gửi chữ."""
        if row.photo_path and os.path.isfile(row.photo_path):
            try:
                self.client.send_photo(chat_id, row.photo_path, row.text)
                return
            except OSError as e:
                log.warning("Không mở được ảnh của tin %s, gửi chữ: %s", row.id, e)
        self.client.send(chat_id, row.text)


def run_delivery(conn, sender, stop, wall=time.time):
    while not stop.is_set():
        try:
            wait = sender.deliver(conn, int(wall()))
        except Exception:
            log.exception("Lỗi khi gửi outbox")
            try:
                conn.rollback()
            except Exception as e:
                log.error("Rollback lỗi: %s", e)
            wait = 30
        stop.wait(wait if wait > 0 else DELIVERY_INTERVAL_S)


# ---------------- dịch vụ ----------------
def configure_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # urllib3 ghi URL từng request (có token) ở mức DEBUG: luôn tắt
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main(argv=None) -> int:
    configure_logging()
    from aiot.config import load_config

    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Bot Telegram của trạm quan trắc không khí")
    parser.add_argument("--config", default=str(root / "config.yaml"))
    parser.add_argument("--env", default=str(root / ".env"))
    args = parser.parse_args(argv)

    cfg = load_config(args.config, args.env)
    token = cfg.secrets.telegram_token
    if not token:
        log.error("Thiếu TELEGRAM_TOKEN trong .env hoặc biến môi trường: bot không chạy")
        return 2
    install_log_redaction(token)
    if not cfg.secrets.allowed_chat_ids:
        log.error("ALLOWED_CHAT_IDS rỗng hoặc không có ID hợp lệ: bot không chạy "
                  "(không có chat nào được phép)")
        return 2

    stop = threading.Event()

    def on_signal(signum, frame):
        log.info("Nhận tín hiệu %s, đang dừng", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.close()

    failed = threading.Event()

    def worker(name, body):
        """Chạy body(conn) với kết nối riêng; luồng chết bất thường thì dừng cả tiến trình
        (thoát mã 1) để systemd khởi động lại, thay vì chạy tiếp một nửa."""
        def target():
            try:
                c = connect(cfg.db_path)
                try:
                    body(c)
                finally:
                    c.close()
            except BaseException:
                log.exception("Luồng %s dừng bất thường", name)
            if not stop.is_set():
                log.error("Luồng %s đã dừng, dừng bot", name)
                failed.set()
                stop.set()
        return threading.Thread(target=target, name=name, daemon=True)

    def polling(c):
        run_polling(c, TelegramClient(token), cfg, stop)

    def delivery(c):
        run_delivery(c, OutboxSender(TelegramClient(token), cfg.secrets.allowed_chat_ids), stop)

    # Luồng long polling có thể đang chờ HTTP tới ~65 giây: để daemon, không chờ nó khi dừng
    poll_thread = worker("telegram-poll", polling)
    send_thread = worker("telegram-send", delivery)
    poll_thread.start()
    send_thread.start()
    log.info("Bot bắt đầu: db=%s, %d chat được phép", cfg.db_path, len(cfg.secrets.allowed_chat_ids))
    while not stop.wait(1):
        pass
    send_thread.join(timeout=25)
    poll_thread.join(timeout=1)
    log.info("Bot đã dừng")
    return 1 if failed.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
