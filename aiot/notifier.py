"""
Chính sách cảnh báo và hàng đợi tin Telegram (bảng outbox).

- Dịch vụ nào cũng chỉ ghi tin vào outbox; bot (Task 8) gửi tin chưa gửi theo thứ tự
  cũ trước, nên mất mạng thì tin được gửi bù khi có mạng lại.
- Giờ yên lặng: khoá state 'quiet_hours' ("start,end", do /quiet ghi) thắng cấu hình
  telegram.quiet_start/quiet_end. Giờ địa phương theo datetime.fromtimestamp(now).
  start == end nghĩa là không có giờ yên lặng.
- Tối đa 1 tin cùng loại mỗi telegram.alert_cooldown_s (khoá 'alert_last:<kind>').
  urgent=True (chỉ dùng cho 'danger', 'gas') bỏ qua giờ yên lặng nhưng không bỏ qua giới hạn này.
- Lỗi cảm biến bị chặn được gửi bù khi hết chặn nếu vẫn còn lỗi (resend_suppressed).
- Mọi hàm ghi có commit=False để gộp vào transaction của người gọi (controller).
- Văn bản tin là tiếng Việt dạng chữ thường, không Markdown.

Tham số cfg của các hàm là Config (dùng cfg.telegram) hoặc trực tiếp TelegramCfg.
"""
import logging
from datetime import datetime
from typing import NamedTuple

from aiot.aqi import LABELS, classify
from aiot.control import fan_on, override_active
from aiot.db import delete_state, get_state, set_state

log = logging.getLogger("aiot.notifier")

ALERT_KINDS = frozenset({
    "aqi_change", "danger", "gas", "sensor_fault", "sensor_recovered",
    "fan_on", "fan_off", "override_expired", "relay_error", "alert_summary",
    # Nắng/mưa (aiot.weather): tin cũ hơn 1 giờ không còn đúng nên cũng bị gộp (bỏ ảnh)
    "weather_sunny", "weather_rain",
})
URGENT_KINDS = frozenset({"danger", "gas"})
# Không bao giờ gộp: 'morning_report', 'reply' (và mọi loại không thuộc ALERT_KINDS)

QUIET_KEY = "quiet_hours"
AQI_NOTIFIED_KEY = "aqi_notified"   # nhóm AQI (index) đã báo gần nhất
AQI_LAST_KEY = "aqi_last"           # nhóm AQI (index) mới tính gần nhất, dùng cho tin tóm tắt
FAULT_PENDING_KEY = "alert_pending:sensor_fault"   # ts lỗi cảm biến chưa báo được


def _last_key(kind):
    return f"alert_last:{kind}"


def _tg(cfg):
    """Mục telegram của Config, hoặc chính cfg nếu đã là TelegramCfg."""
    return getattr(cfg, "telegram", cfg)


def _fmt_time(ts):
    return datetime.fromtimestamp(int(ts)).strftime("%H:%M %d/%m")


def _fmt_pm(value):
    """40.04 -> '40,0' (dấu phẩy thập phân như tài liệu tiếng Việt)."""
    return f"{value:.1f}".replace(".", ",")


class OutboxRow(NamedTuple):
    id: int
    created_at: int
    kind: str
    text: str
    photo_path: str | None = None   # ảnh gửi kèm (text làm chú thích); None: chỉ chữ


# ---------------- outbox ----------------
def enqueue(conn, kind, text, now, *, photo_path=None, commit=True):
    """Thêm một tin chờ gửi. Không kiểm tra chính sách (dùng send_alert cho cảnh báo).
    photo_path: ảnh JPEG gửi kèm; file mất khi tới lúc gửi thì bot gửi chữ."""
    conn.execute("INSERT INTO outbox (created_at, kind, text, photo_path) VALUES (?, ?, ?, ?)",
                 (int(now), kind, text, photo_path))
    if commit:
        conn.commit()


def pending_messages(conn):
    """Tin chưa gửi, cũ trước."""
    rows = conn.execute(
        "SELECT id, created_at, kind, text, photo_path FROM outbox WHERE sent_at IS NULL "
        "ORDER BY created_at, id").fetchall()
    return [OutboxRow(*r) for r in rows]


def mark_sent(conn, id, now, *, commit=True):
    conn.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (int(now), int(id)))
    if commit:
        conn.commit()


def collapse_stale_alerts(conn, now, max_age_s=3600, *, commit=True):
    """Gộp các cảnh báo chưa gửi cũ hơn max_age_s (thường sau khi mất mạng) thành MỘT tin
    'alert_summary'. Tin gốc được đánh dấu sent_at = now (giữ lại làm dấu vết, không xoá).
    Báo cáo sáng, trả lời lệnh và tin tóm tắt có sẵn không bị gộp. Ảnh của tin bị gộp
    (nắng/mưa) không được gửi."""
    kinds = sorted(ALERT_KINDS - {"alert_summary"})
    marks = ",".join("?" * len(kinds))
    cutoff = int(now) - int(max_age_s)
    try:
        rows = conn.execute(
            f"SELECT id, created_at FROM outbox WHERE sent_at IS NULL AND created_at < ? "
            f"AND kind IN ({marks}) ORDER BY created_at, id", (cutoff, *kinds)).fetchall()
        if not rows:
            return
        first, last = rows[0][1], rows[-1][1]
        text = (f"Trong lúc không gửi được tin có {len(rows)} cảnh báo cũ, "
                f"từ {_fmt_time(first)} tới {_fmt_time(last)}. Đã gộp lại để không gửi dồn.")
        level = get_state(conn, AQI_LAST_KEY)
        if level is not None and level.isdigit() and int(level) < len(LABELS):
            text += f" Chất lượng không khí gần nhất: {LABELS[int(level)]}."
        text += " Gửi /now để xem số liệu hiện tại."
        conn.executemany("UPDATE outbox SET sent_at = ? WHERE id = ?",
                         [(int(now), r[0]) for r in rows])
        # created_at = tin gốc mới nhất để giữ thứ tự thời gian với các tin khác
        enqueue(conn, "alert_summary", text, last, commit=False)
        if commit:
            conn.commit()
    except BaseException:
        if commit:
            conn.rollback()
        raise


# ---------------- chính sách gửi ----------------
def in_quiet_hours(hour, start, end):
    """hour thuộc [start, end) theo vòng 24 giờ; start == end: không có giờ yên lặng."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end          # vắt qua nửa đêm, ví dụ 22 -> 6


def quiet_window(conn, cfg):
    """(start, end) giờ yên lặng đang áp dụng: khoá quiet_hours thắng cấu hình."""
    tg = _tg(cfg)
    text = get_state(conn, QUIET_KEY)
    if text is not None:
        try:
            start, end = (int(p) for p in text.split(","))
            if 0 <= start < 24 and 0 <= end < 24:
                return start, end
        except ValueError:
            pass
        log.warning("Khoá quiet_hours sai định dạng (%r), dùng cấu hình", text)
    return tg.quiet_start, tg.quiet_end


def should_alert(kind, now, conn, cfg, urgent=False):
    """Có được gửi cảnh báo loại kind lúc now không (chỉ đọc, không ghi gì)."""
    last = get_state(conn, _last_key(kind))
    if last is not None and int(now) - int(last) < _tg(cfg).alert_cooldown_s:
        return False
    if urgent:
        return True
    start, end = quiet_window(conn, cfg)
    return not in_quiet_hours(datetime.fromtimestamp(int(now)).hour, start, end)


def send_alert(conn, kind, text, now, cfg, urgent=False, *, commit=True):
    """Ghi cảnh báo vào outbox nếu chính sách cho phép và bắt đầu giới hạn 30 phút.
    Trả về True nếu đã ghi. 'sensor_fault' bị chặn thì để lại dấu để gửi bù sau."""
    try:
        if not should_alert(kind, now, conn, cfg, urgent):
            if kind == "sensor_fault" and get_state(conn, FAULT_PENDING_KEY) is None:
                set_state(conn, FAULT_PENDING_KEY, int(now), commit=False)
            sent = False
        else:
            enqueue(conn, kind, text, now, commit=False)
            set_state(conn, _last_key(kind), int(now), commit=False)
            sent = True
        if commit:
            conn.commit()
        return sent
    except BaseException:
        if commit:
            conn.rollback()
        raise


def resend_suppressed(conn, now, cfg, fault_active, *, commit=True):
    """Gọi mỗi vòng: lỗi cảm biến từng bị chặn thì gửi khi được phép nếu vẫn còn lỗi;
    hết lỗi thì xoá dấu (không báo lỗi đã qua)."""
    since = get_state(conn, FAULT_PENDING_KEY)
    if since is None:
        return
    try:
        if not fault_active:
            delete_state(conn, FAULT_PENDING_KEY, commit=False)
        elif should_alert("sensor_fault", now, conn, cfg):
            text = (f"Lỗi cảm biến: không nhận được dữ liệu PM2.5 từ {_fmt_time(int(since))} "
                    f"tới giờ. Quạt giữ nguyên trạng thái; kiểm tra dây và nguồn PMS7003.")
            enqueue(conn, "sensor_fault", text, now, commit=False)
            set_state(conn, _last_key("sensor_fault"), int(now), commit=False)
            delete_state(conn, FAULT_PENDING_KEY, commit=False)
        else:
            return
        if commit:
            conn.commit()
    except BaseException:
        if commit:
            conn.rollback()
        raise


def check_aqi_change(conn, pm25_15min, now, cfg, *, commit=True):
    """So nhóm AQI của PM2.5 TB 15 phút với nhóm đã báo. Lần đầu chỉ ghi nhận.
    Bị chặn thì không ghi nhận, nên lần được phép sau so lại và chỉ gửi nếu vẫn khác."""
    if pm25_15min is None:
        return
    level = classify(pm25_15min)
    try:
        set_state(conn, AQI_LAST_KEY, level.index, commit=False)
        notified = get_state(conn, AQI_NOTIFIED_KEY)
        if notified is None:
            set_state(conn, AQI_NOTIFIED_KEY, level.index, commit=False)
        elif notified != str(level.index):
            old = LABELS[int(notified)] if notified.isdigit() and int(notified) < len(LABELS) else "?"
            text = (f"Chất lượng không khí đổi nhóm: {old} -> {level.label} "
                    f"(PM2.5 trung bình 15 phút {_fmt_pm(pm25_15min)} µg/m³).")
            if send_alert(conn, "aqi_change", text, now, cfg, commit=False):
                set_state(conn, AQI_NOTIFIED_KEY, level.index, commit=False)
        if commit:
            conn.commit()
    except BaseException:
        if commit:
            conn.rollback()
        raise


# ---------------- văn bản tin cho Event của controller ----------------
_FAN_ON_REASONS = {
    "enter": "bụi mịn PM2.5 trung bình cao kéo dài",
    "danger": "bụi mịn ở mức khẩn cấp",
    "gas": "cảm biến khí MQ135 báo vượt ngưỡng",
    "proactive": "dự báo 3 giờ tới bụi mịn tăng cao",
}


def _on_off(on):
    return "bật" if on else "tắt"


def event_message(ev, st, inp, now):
    """(văn bản, urgent) cho một Event của controller, hoặc None nếu không cần báo.
    st là trạng thái SAU bước điều khiển, inp là Inputs của bước đó."""
    now = int(now)
    manual = override_active(st, now)
    on = fan_on(st, now)
    until = f" tới {_fmt_time(st.override_until)}" if manual else ""

    if ev.kind == "danger":
        value = (f", khoảng {_fmt_pm(inp.danger_level)} µg/m³"
                 if inp is not None and inp.danger_level is not None else "")
        text = (f"CẢNH BÁO KHẨN CẤP: bụi mịn PM2.5 trong nhà đang ở mức khẩn cấp{value}. "
                "Đóng cửa sổ, hạn chế ra ngoài và vận động mạnh, người nhạy cảm nên đeo khẩu trang. ")
        if manual:
            text += f"Quạt lọc đang {_on_off(on)} theo ghi đè thủ công{until}"
            text += "; gửi /mode auto để quạt tự bật." if not on else "."
        else:
            text += "Quạt lọc được bật tự động."
        return text, True

    if ev.kind == "gas":
        text = ("CẢNH BÁO KHÍ: cảm biến MQ135 báo vượt ngưỡng. "
                "Mở cửa cho thoáng và kiểm tra nguồn khói, khí gas trong nhà. ")
        if manual:
            text += f"Quạt lọc đang {_on_off(on)} theo ghi đè thủ công{until}"
            text += "; gửi /mode auto để quạt tự bật." if not on else "."
        else:
            text += "Quạt lọc được bật tự động."
        return text, True

    if ev.kind == "fan_on":
        reason = _FAN_ON_REASONS.get(ev.detail, ev.detail or "không khí xấu")
        if ev.detail == "enter" and inp is not None and inp.mean_enter is not None:
            reason += f" ({_fmt_pm(inp.mean_enter)} µg/m³)"
        if manual:
            return (f"Luật tự động chuyển sang chế độ bảo vệ ({reason}), nhưng ghi đè thủ công "
                    f"đang giữ quạt {_on_off(on)}{until}. Gửi /mode auto để về tự động."), False
        return f"Đã bật quạt lọc: {reason}.", False

    if ev.kind == "fan_off":
        head = ("Cảm biến khí MQ135 đã hết báo vượt ngưỡng" if ev.detail == "exit_gas"
                else "Không khí đã tốt lại")
        value = (f" (PM2.5 trung bình {_fmt_pm(inp.mean_exit)} µg/m³)"
                 if inp is not None and inp.mean_exit is not None else "")
        if manual:
            return (f"{head}{value}. Luật tự động về chế độ bình thường, nhưng "
                    f"ghi đè thủ công đang giữ quạt {_on_off(on)}{until}."), False
        return f"{head}{value}, đã tắt quạt lọc.", False

    if ev.kind == "sensor_fault":
        return ("Lỗi cảm biến: không nhận được dữ liệu PM2.5. Quạt giữ nguyên trạng thái "
                f"(đang {_on_off(on)}); kiểm tra dây và nguồn PMS7003."), False

    if ev.kind == "sensor_recovered":
        return ("Cảm biến PM2.5 đã có dữ liệu trở lại. Luật tự động chạy lại, "
                f"quạt đang {_on_off(on)}."), False

    if ev.kind == "override_expired":
        return f"Ghi đè thủ công đã hết hạn, quạt về chế độ tự động (đang {_on_off(on)}).", False

    return None
