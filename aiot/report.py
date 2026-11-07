"""
Báo cáo không khí: tổng hợp 24 giờ, lời khuyên (Gemini hoặc mẫu), dự báo, quạt.

- summarize(): chỉ số liệu tổng hợp từ readings (valid=1), sự kiện quạt, control_state và
  dự báo mới nhất. Dict này cũng là thứ duy nhất gửi lên Gemini (lọc lại bằng
  aiot.advice.ALLOWED_KEYS); không có device id, chat id, token hay mẫu thô.
- Gemini tối đa gemini.max_calls_per_day lần mỗi ngày (giờ địa phương), đếm ở khoá state
  'gemini_calls:<YYYY-MM-DD>'; mỗi lần thử đều tính, kể cả lỗi; /report cũng tính. Hết lượt
  thì dùng lời khuyên mẫu.
- Báo cáo sáng: khoá 'morning_report_date' (ngày địa phương) chống gửi trùng; tin
  'morning_report' và khoá được ghi trong MỘT transaction. Đồng hồ chưa đồng bộ NTP thì vẫn
  gửi (kèm ghi chú), không kiểm tra và không ghi khoá ngày (ngày có thể sai).
- Lập báo cáo lỗi bất ngờ thì vẫn gửi một tin ngắn: bản tin sáng không bao giờ bị bỏ.
- Gemini chỉ viết lời khuyên; quạt do luật tất định trong aiot.control điều khiển.

Chạy một lần (timer 06:30): cd ~/BTL_IOT && venv/bin/python -m aiot.report
Đồng hồ chưa đồng bộ: chờ tối đa 15 phút (kiểm tra mỗi 30 giây) rồi vẫn gửi. Luôn thoát 0
khi đã ghi được tin vào outbox; bot gửi tin (gửi bù khi có mạng lại).
"""
import argparse
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from aiot.advice import get_advice, template_advice
from aiot.aqi import LABELS, classify
from aiot.collector import is_clock_trusted
from aiot.control import ControlState, fan_on, override_active
from aiot.db import get_state, set_state
from aiot.forecast import MODEL_NAMES, latest_rows
from aiot.notifier import enqueue

log = logging.getLogger("aiot.report")

DAY_S = 86400
HOUR_S = 3600
WINDOW_S = 300
EXPECTED_WINDOWS = DAY_S // WINDOW_S        # 288 cửa sổ 5 phút trong 24 giờ
MIN_WINDOWS_PER_HOUR = 6                    # giờ cần ít nhất 6/12 cửa sổ PM2.5 mới xếp nhóm
DEFAULT_FORECAST_MAX_AGE_S = 5400
MORNING_KEY = "morning_report_date"
CALLS_PREFIX = "gemini_calls:"
STATE_KEY = "control_state"                 # cùng khoá với aiot.controller
CLOCK_WAIT_S = 900
CLOCK_POLL_S = 30
CLOCK_NOTE = ("Lưu ý: đồng hồ của Pi chưa đồng bộ qua mạng (NTP), giờ và số liệu theo giờ "
              "trong báo cáo có thể sai.")
FALLBACK_TEXT = ("Báo cáo không khí: không lập được báo cáo đầy đủ do lỗi phần mềm trên Pi "
                 "(xem journalctl -u aiot-morning). Gửi /now để xem số liệu hiện tại.")
_OVERRIDE_RE = re.compile(r"set fan=(on|off) until=(\d+)")


# ---------------- định dạng ----------------
def _local_date(ts):
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")


def _num(value, digits=1):
    return f"{value:.{digits}f}".replace(".", ",")


def _label(value):
    try:
        return classify(value).label
    except ValueError:
        return None


def _round(value, digits=1):
    return None if value is None else round(float(value), digits)


# ---------------- quạt ----------------
def fan_on_seconds(conn, start, end):
    """Số giây quạt bật trong [start, end), dựng lại từ nhật ký sự kiện của controller:
    fan_on/fan_off (chế độ BẢO VỆ/BÌNH THƯỜNG), override ('set fan=on|off until=...',
    'cleared ...'), override_expired, state_corrupt (về BÌNH THƯỜNG). Ghi đè hết hạn theo
    until kể cả khi không có sự kiện. Là ước tính: không tính lúc controller dừng."""
    start, end = int(start), int(end)
    rows = conn.execute(
        "SELECT ts, kind, detail FROM events WHERE ts < ? AND kind IN "
        "('fan_on', 'fan_off', 'override', 'override_expired', 'state_corrupt') "
        "ORDER BY ts, rowid", (end,)).fetchall()
    protect = False
    override = None                     # (bật?, until) hoặc None

    def on_between(a, b):
        """Số giây bật trong [a, b) với trạng thái hiện tại, cắt theo [start, end)."""
        a, b = max(a, start), min(b, end)
        if b <= a:
            return 0
        if override is None:
            return b - a if protect else 0
        fan, until = override
        cut = min(max(until, a), b)
        total = (cut - a) if fan else 0             # đoạn còn ghi đè
        total += (b - cut) if protect else 0        # đoạn sau khi ghi đè hết hạn
        return total

    total, t = 0, None
    for ts, kind, detail in rows:
        ts = int(ts)
        if t is not None:
            total += on_between(t, ts)
        t = ts
        if override is not None and ts >= override[1]:
            override = None
        if kind == "fan_on":
            protect = True
        elif kind == "fan_off":
            protect = False
        elif kind == "state_corrupt":
            protect, override = False, None
        elif kind == "override_expired":
            override = None
        elif kind == "override":
            m = _OVERRIDE_RE.search(detail or "")
            if m:
                override = (m.group(1) == "on", int(m.group(2)))
            elif (detail or "").startswith("cleared"):
                override = None
    if t is None:
        t = start
    total += on_between(t, end)
    return total


def _control_now(conn, now):
    text = get_state(conn, STATE_KEY)
    if text is None:
        st = ControlState()
    else:
        try:
            st = ControlState.from_json(text)
        except ValueError:
            return None, None, None
    mode = "BẢO VỆ" if st.mode == "PROTECT" else "BÌNH THƯỜNG"
    return bool(fan_on(st, now)), mode, bool(override_active(st, now))


# ---------------- tổng hợp ----------------
def summarize(conn, now, forecast_max_age_s=DEFAULT_FORECAST_MAX_AGE_S) -> dict:
    """Số liệu tổng hợp 24 giờ trước now (readings valid=1, ts trong [now-24h, now)).
    Chỉ chứa các khoá trong aiot.advice.ALLOWED_KEYS."""
    now = int(now)
    start = now - DAY_S
    rows = conn.execute(
        "SELECT ts, pm25, temp, hum, mq135_do FROM readings "
        "WHERE valid = 1 AND ts >= ? AND ts < ? ORDER BY ts", (start, now)).fetchall()
    pm = [(int(ts), float(v)) for ts, v, _, _, _ in rows if v is not None]
    temps = [float(r[2]) for r in rows if r[2] is not None]
    hums = [float(r[3]) for r in rows if r[3] is not None]

    # 24 khối 1 giờ liên tiếp kết thúc tại now
    blocks = {}
    for ts, v in pm:
        blocks.setdefault((ts - start) // HOUR_S, []).append(v)
    aqi_hours = {label: 0 for label in LABELS}
    for values in blocks.values():
        if len(values) >= MIN_WINDOWS_PER_HOUR:
            label = _label(sum(values) / len(values))
            if label:
                aqi_hours[label] += 1

    last_hour = [v for ts, v in pm if ts >= now - HOUR_S]
    pm25_mean = sum(v for _, v in pm) / len(pm) if pm else None
    last_mean = sum(last_hour) / len(last_hour) if last_hour else None
    fan_now, mode, manual = _control_now(conn, now)

    forecast = None
    found = latest_rows(conn, now, forecast_max_age_s)
    if found is not None and found[2]:
        made_at, model, preds = found
        values = [round(v, 1) for _, v in preds]
        forecast = {
            "model": model,
            "made_at_local": datetime.fromtimestamp(made_at).strftime("%H:%M"),
            "hours": [datetime.fromtimestamp(made_at + (h - 1) * HOUR_S).strftime("%H:00")
                      for h, _ in preds],
            "pm25_next_6h": values,
            "aqi_next_6h": [_label(v) for v in values],
        }

    return {
        "period_hours": 24,
        "has_data": bool(pm),
        "data_coverage": round(min(1.0, len(rows) / EXPECTED_WINDOWS), 3),
        "pm25_mean": _round(pm25_mean),
        "pm25_min": _round(min(v for _, v in pm)) if pm else None,
        "pm25_max": _round(max(v for _, v in pm)) if pm else None,
        "aqi_24h": _label(pm25_mean) if pm25_mean is not None else None,
        "pm25_last_1h_mean": _round(last_mean),
        "aqi_now": _label(last_mean) if last_mean is not None else None,
        "aqi_hours": aqi_hours,
        "temp_mean": _round(sum(temps) / len(temps)) if temps else None,
        "hum_mean": _round(sum(hums) / len(hums), 0) if hums else None,
        "mq135_exceeded_windows": sum(1 for r in rows if r[4] == 0),
        "fan_on_now": fan_now,
        "control_mode": mode,
        "manual_override": manual,
        "fan_on_minutes_24h": round(fan_on_seconds(conn, start, now) / 60),
        "forecast": forecast,
    }


# ---------------- giới hạn số lần gọi Gemini ----------------
def take_gemini_slot(conn, now, limit) -> bool:
    """Giữ một lượt gọi Gemini của ngày (giờ địa phương). Hết lượt thì False.
    Một câu lệnh UPSERT có điều kiện nên bot và báo cáo sáng chạy cùng lúc không vượt."""
    if int(limit) <= 0:
        return False
    key = CALLS_PREFIX + _local_date(now)
    cur = conn.execute(
        "INSERT INTO state (key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET "
        "value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE CAST(value AS INTEGER) < ?",
        (key, int(limit)))
    conn.commit()
    return cur.rowcount == 1


def _advice(conn, summary, now, cfg, client):
    if (summary["has_data"] and (cfg.gemini.model or "").strip()
            and (client is not None or cfg.secrets.gemini_api_key)):
        if take_gemini_slot(conn, now, cfg.gemini.max_calls_per_day):
            return get_advice(summary, cfg, client=client)
        log.info("Đã dùng hết %d lượt gọi Gemini hôm nay: dùng lời khuyên mẫu",
                 cfg.gemini.max_calls_per_day)
    return template_advice(summary)     # không có số liệu/model/key: không gọi, không tính lượt


# ---------------- văn bản báo cáo ----------------
def build_report(conn, now, cfg, client=None, *, morning=False) -> str:
    """Văn bản báo cáo 24 giờ trước now. Không enqueue, không đụng khoá báo cáo sáng.
    Có thể gọi Gemini (tính vào giới hạn mỗi ngày)."""
    now = int(now)
    s = summarize(conn, now, cfg.control.forecast_max_age_s)
    a = _advice(conn, s, now, cfg, client)

    title = "Báo cáo không khí sáng" if morning else "Báo cáo không khí"
    lines = [f"{title} {datetime.fromtimestamp(now).strftime('%H:%M %d/%m/%Y')}", "",
             a.headline,
             a.advice + (" (lời khuyên mẫu)" if a.source == "template" else ""), "",
             "24 giờ qua:"]
    if s["has_data"]:
        lines.append(f"- PM2.5 trung bình {_num(s['pm25_mean'])} µg/m³ ({s['aqi_24h']}), "
                     f"thấp nhất {_num(s['pm25_min'])}, cao nhất {_num(s['pm25_max'])}")
        if s["pm25_last_1h_mean"] is not None:
            lines.append(f"- 1 giờ gần nhất: {_num(s['pm25_last_1h_mean'])} µg/m³ ({s['aqi_now']})")
        hours = [f"{label} {n}" for label, n in s["aqi_hours"].items() if n]
        if hours:
            lines.append("- Số giờ theo nhóm AQI: " + ", ".join(hours))
    else:
        lines.append("- Thiếu dữ liệu: không có số liệu PM2.5 hợp lệ nào. Kiểm tra dịch vụ "
                     "collector và cảm biến PMS7003.")
    climate = []
    if s["temp_mean"] is not None:
        climate.append(f"nhiệt độ trung bình {_num(s['temp_mean'])} °C")
    if s["hum_mean"] is not None:
        climate.append(f"độ ẩm trung bình {_num(s['hum_mean'], 0)}%")
    if climate:
        text = ", ".join(climate)
        lines.append("- " + text[0].upper() + text[1:])
    lines.append(f"- Dữ liệu: có {int(s['data_coverage'] * 100 + 1e-9)}% số cửa sổ 5 phút")

    f = s["forecast"]
    lines.append("")
    if f is None:
        lines.append("Dự báo PM2.5 6 giờ tới: chưa có dự báo.")
    else:
        name = MODEL_NAMES.get(f["model"])
        model_text = f"{f['model']} - {name}" if name else f["model"]
        lines.append(f"Dự báo PM2.5 6 giờ tới (lập lúc {f['made_at_local']}, "
                     f"mô hình {model_text}):")
        for hour, value, label in zip(f["hours"], f["pm25_next_6h"], f["aqi_next_6h"]):
            lines.append(f"- {hour}: {_num(value)} µg/m³" + (f" ({label})" if label else ""))

    lines.append("")
    if s["fan_on_now"] is None:
        fan = "Quạt lọc: không đọc được trạng thái điều khiển"
    else:
        fan = (f"Quạt lọc: đang {'bật' if s['fan_on_now'] else 'tắt'} "
               f"(luật tự động: {s['control_mode']}"
               + (", đang ghi đè thủ công" if s["manual_override"] else "") + ")")
    lines.append(f"{fan}; bật khoảng {s['fan_on_minutes_24h']} phút trong 24 giờ qua.")
    if s["mq135_exceeded_windows"]:
        lines.append(f"MQ135 (chỉ báo khí, không phải nồng độ CO₂): vượt ngưỡng ở "
                     f"{s['mq135_exceeded_windows']} cửa sổ 5 phút.")
    return "\n".join(lines)


# ---------------- báo cáo sáng ----------------
def send_morning_report(conn, now, cfg, client=None, *, clock_synced=True) -> bool:
    """Ghi báo cáo sáng vào outbox. False nếu hôm nay (ngày địa phương) đã gửi.
    clock_synced False: vẫn gửi kèm ghi chú, không kiểm tra/ghi khoá ngày."""
    now = int(now)
    date = _local_date(now)
    if clock_synced and get_state(conn, MORNING_KEY) == date:
        log.info("Báo cáo sáng ngày %s đã gửi, bỏ qua", date)
        return False
    try:
        text = build_report(conn, now, cfg, client, morning=True)
    except Exception:
        log.exception("Lỗi khi lập báo cáo sáng, gửi tin ngắn thay thế")
        conn.rollback()
        text = FALLBACK_TEXT
    if not clock_synced:
        text += "\n" + CLOCK_NOTE
    try:
        enqueue(conn, "morning_report", text, now, commit=False)
        if clock_synced:
            set_state(conn, MORNING_KEY, date, commit=False)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    log.info("Đã đưa báo cáo sáng ngày %s vào hàng đợi", date)
    return True


def wait_for_clock(clock_synced, *, sleep=time.sleep, monotonic=time.monotonic,
                   max_wait_s=CLOCK_WAIT_S, poll_s=CLOCK_POLL_S) -> bool:
    """Chờ đồng hồ đồng bộ NTP tối đa max_wait_s giây, kiểm tra mỗi poll_s giây."""
    begin = monotonic()
    while True:
        if clock_synced():
            return True
        if monotonic() - begin >= max_wait_s:
            return False
        sleep(poll_s)


def main(argv=None, *, sleep=time.sleep, monotonic=time.monotonic) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)       # không ghi URL request
    from aiot.config import load_config
    from aiot.db import connect, init_schema

    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Gửi báo cáo không khí sáng (chạy một lần)")
    parser.add_argument("--config", default=str(root / "config.yaml"))
    parser.add_argument("--env", default=str(root / ".env"))
    args = parser.parse_args(argv)

    cfg = load_config(args.config, args.env)
    synced = is_clock_trusted()
    if not synced:
        log.warning("Đồng hồ Pi chưa đồng bộ NTP: chờ tối đa %d phút", CLOCK_WAIT_S // 60)
        synced = wait_for_clock(lambda: is_clock_trusted(), sleep=sleep, monotonic=monotonic)
        if not synced:
            log.warning("Vẫn chưa đồng bộ giờ: gửi báo cáo kèm ghi chú")
    conn = connect(cfg.db_path)
    try:
        init_schema(conn)
        send_morning_report(conn, int(time.time()), cfg, clock_synced=synced)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
