"""
Dịch vụ uplink: đẩy dữ liệu của trạm lên web trên cloud và nhận lệnh từ web.

- Mỗi cloud.interval_s giây gọi POST <cloud.url>/api/ingest (header Bearer INGEST_TOKEN):
  snapshot hiện tại (mẫu mới nhất, AQI, nhãn AI + xác suất, quạt, còi, chế độ), history
  trung bình cloud.history_step_s giây (chỉ cửa sổ đã đủ), dự báo mới, sự kiện mới, và kết
  quả các lệnh web đã xử lý. Pi chỉ kết nối ra ngoài, không mở port.
- Mốc đã gửi (history, dự báo, sự kiện) lưu ở khoá state 'uplink_cursor' và chỉ tiến lên
  sau khi cloud trả 200, nên mất mạng thì lần sau gửi bù (tối đa MAX_HISTORY dòng mỗi lần).
- Lệnh web được ghi qua aiot.commands (đúng đường của lệnh Telegram): uplink không chạm
  GPIO, controller áp dụng ở vòng kế tiếp. Lệnh cũ hơn cloud.command_max_age_s (tính bằng
  đồng hồ của server: age_s) bị từ chối. Mỗi id chỉ áp dụng một lần: id đã xử lý và kết
  quả lưu ở khoá 'uplink_done' (200 id gần nhất), để cloud gửi lại sau khi mất mạng hay
  Pi khởi động lại không làm lệnh chạy hai lần.
- Không gửi chat id Telegram hay IP: chi tiết sự kiện bị bỏ các trường chat=, ip=, user=.
- Lỗi mạng/HTTP: chỉ ghi log và chờ lâu dần tới BACKOFF_MAX_S. Không ảnh hưởng collector
  hay controller. cloud.enabled false, thiếu url hoặc INGEST_TOKEN: thoát mã 2.

Chạy: cd ~/BTL_IOT && venv/bin/python -m aiot.uplink
"""
import argparse
import json
import logging
import re
import signal
import threading
import time
from pathlib import Path

from aiot import commands
from aiot.aqi import classify
from aiot.control import fan_on, override_active
from aiot.db import connect, get_state, init_schema, set_state

log = logging.getLogger("aiot.uplink")

CURSOR_KEY = "uplink_cursor"
DONE_KEY = "uplink_done"
LAST_OK_KEY = "uplink_last_ok"        # giây epoch lần gửi thành công gần nhất (healthcheck đọc)
SOURCE_KEY = "source_state"
ALARM_KEY = "alarm_state"
MUTE_KEY = "buzzer_mute_until"
HEARTBEAT_KEY = "controller_heartbeat"
MAX_HISTORY = 720                     # 12 giờ bước 1 phút mỗi lần gửi
MAX_EVENTS = 200
FIRST_HISTORY_S = 24 * 3600           # lần đầu gửi history 24 giờ qua
FIRST_EVENTS = 50
DONE_KEEP = 200
BACKOFF_MAX_S = 60
_PRIVATE_RE = re.compile(r"\s*\b(chat|ip|user)=\S+")


# ---------------- đọc dữ liệu ----------------
def _json_state(conn, key):
    try:
        return json.loads(get_state(conn, key) or "null")
    except ValueError:
        return None


def load_cursor(conn):
    data = _json_state(conn, CURSOR_KEY)
    return data if isinstance(data, dict) else {}


def build_snapshot(conn, cfg, now):
    now = int(now)
    snap = {"pi_time": now, "loop_s": cfg.control.loop_s}
    row = conn.execute("SELECT ts, pm1, pm25, pm10, temp, hum, lux FROM raw_readings "
                       "ORDER BY ts DESC LIMIT 1").fetchone()
    if row is not None:
        snap.update(dict(zip(("ts", "pm1", "pm25", "pm10", "temp", "hum", "lux"), row)))
        try:
            level = classify(row[2])
            snap["aqi"] = {"index": level.index, "label": level.label}
        except ValueError:
            snap["aqi"] = None

    src = _json_state(conn, SOURCE_KEY) or {}
    pred = conn.execute("SELECT ts, raw_label, proba, model FROM source_predictions "
                        "ORDER BY ts DESC LIMIT 1").fetchone()
    proba = None
    if pred is not None and pred[2]:
        try:
            proba = json.loads(pred[2])
        except ValueError:
            proba = None
    last_ts = src.get("last_ts") if isinstance(src, dict) else None
    fresh = isinstance(last_ts, int) and now - last_ts <= max(60, 6 * cfg.control.loop_s)
    snap["source"] = {"enabled": cfg.source.enabled, "label": src.get("label"),
                      "since": src.get("since"), "last_ts": last_ts, "fresh": fresh,
                      "proba": proba if fresh else None,
                      "raw_label": pred[1] if pred is not None else None,
                      "model": pred[3] if pred is not None else None}

    st = commands.control_state(conn)
    fan_ov = None
    if st is not None and override_active(st, now):
        fan_ov = {"on": st.override_fan, "until": st.override_until}
    bz_ov = commands.buzzer_override(conn, now)
    snap["control"] = {
        "mode": "manual" if (fan_ov is not None or bz_ov is not None) else "auto",
        "rule_mode": st.mode if st is not None else None,
        "fan_on": fan_on(st, now) if st is not None else None,
        "fan_override": fan_ov,
        "fault": bool(st.fault_active) if st is not None else None,
    }
    alarm = _json_state(conn, ALARM_KEY) or {}
    try:
        mute_until = int(get_state(conn, MUTE_KEY) or 0)
    except ValueError:
        mute_until = 0
    muted = mute_until > now
    snap["buzzer"] = {
        "enabled": cfg.buzzer.enabled,
        "override": {"on": bz_ov[0], "until": bz_ov[1]} if bz_ov is not None else None,
        "reason": alarm.get("reason") if isinstance(alarm, dict) else None,
        "sounding": bool(isinstance(alarm, dict) and alarm.get("reason")
                         and alarm.get("reason") != "manual" and not muted and bz_ov is None),
        "muted_until": mute_until if muted else None,
        "manual_max_s": cfg.buzzer.manual_max_s,
    }
    try:
        beat = int(get_state(conn, HEARTBEAT_KEY))
        snap["controller_age_s"] = now - beat
    except (TypeError, ValueError):
        snap["controller_age_s"] = None
    return snap


def _label_at(conn, start, end):
    """Nhãn AI xuất hiện nhiều nhất trong [start, end), hoặc None."""
    row = conn.execute("SELECT label, count(*) AS n FROM source_predictions "
                       "WHERE ts >= ? AND ts < ? AND label IS NOT NULL "
                       "GROUP BY label ORDER BY n DESC LIMIT 1", (start, end)).fetchone()
    return row[0] if row else None


def build_history(conn, since_ts, now, step_s):
    """Trung bình theo bước step_s của raw_readings từ since_ts (căn theo bước) tới cửa sổ
    đã đủ cuối cùng (< now). Trả về (danh sách dòng, mốc tiếp theo)."""
    step_s = max(10, int(step_s))
    start = (int(since_ts) // step_s) * step_s
    end = (int(now) // step_s) * step_s             # cửa sổ [end, ...) chưa đủ
    rows = conn.execute(
        "SELECT (ts / ?) * ? AS b, avg(pm1), avg(pm25), avg(pm10), avg(temp), avg(hum), avg(lux) "
        "FROM raw_readings WHERE ts >= ? AND ts < ? GROUP BY b ORDER BY b LIMIT ?",
        (step_s, step_s, start, end, MAX_HISTORY)).fetchall()
    out = []
    for b, pm1, pm25, pm10, temp, hum, lux in rows:
        out.append({"ts": int(b), "pm1": pm1, "pm25": pm25, "pm10": pm10, "temp": temp,
                    "hum": hum, "lux": lux, "label": _label_at(conn, b, b + step_s)})
    nxt = (out[-1]["ts"] + step_s) if len(out) == MAX_HISTORY else end
    return out, nxt


def build_forecast(conn, after_made_at):
    row = conn.execute("SELECT max(made_at) FROM forecasts").fetchone()
    made_at = row[0] if row else None
    if made_at is None or (after_made_at is not None and made_at <= after_made_at):
        return None
    rows = conn.execute("SELECT horizon_h, pm25_pred, model FROM forecasts WHERE made_at = ? "
                        "ORDER BY horizon_h", (made_at,)).fetchall()
    return {"made_at": int(made_at), "model": rows[0][2] if rows else None,
            "values": [r[1] for r in rows]}


def sanitize(detail):
    return _PRIVATE_RE.sub("", detail or "").strip()


def build_events(conn, after_rowid):
    if after_rowid is None:
        rows = conn.execute("SELECT rowid, ts, kind, detail FROM events ORDER BY rowid DESC "
                            "LIMIT ?", (FIRST_EVENTS,)).fetchall()[::-1]
    else:
        rows = conn.execute("SELECT rowid, ts, kind, detail FROM events WHERE rowid > ? "
                            "ORDER BY rowid LIMIT ?", (after_rowid, MAX_EVENTS)).fetchall()
    return [{"id": r[0], "ts": r[1], "kind": r[2], "detail": sanitize(r[3])} for r in rows]


def build_payload(conn, cfg, now, acks):
    """(payload, mốc mới nếu gửi thành công)."""
    cur = load_cursor(conn)
    since = cur.get("history_ts")
    if not isinstance(since, int):
        since = int(now) - FIRST_HISTORY_S
    history, next_ts = build_history(conn, since, now, cfg.cloud.history_step_s)
    forecast = build_forecast(conn, cur.get("forecast_at"))
    events = build_events(conn, cur.get("event_rowid"))
    new_cur = dict(cur, history_ts=next_ts)
    if forecast is not None:
        new_cur["forecast_at"] = forecast["made_at"]
    if events:
        new_cur["event_rowid"] = events[-1]["id"]
    elif cur.get("event_rowid") is None:
        row = conn.execute("SELECT max(rowid) FROM events").fetchone()
        new_cur["event_rowid"] = row[0] or 0
    payload = {"snapshot": build_snapshot(conn, cfg, now), "history": history,
               "forecast": forecast, "events": events, "acks": list(acks)}
    return payload, new_cur


# ---------------- lệnh từ web ----------------
def load_done(conn):
    data = _json_state(conn, DONE_KEY)
    return data if isinstance(data, dict) else {}


def apply_command(conn, cfg, now, cmd, done):
    """Áp dụng một lệnh web. Trả về ack {"id", "status", "detail"} hoặc None nếu sai định
    dạng hoàn toàn (không có id). Ghi 'uplink_done' cùng transaction với lệnh."""
    cid = cmd.get("id") if isinstance(cmd, dict) else None
    if not isinstance(cid, str) or not cid or len(cid) > 64:
        return None
    if cid in done:                         # đã xử lý: chỉ báo lại kết quả cũ
        status, detail = done[cid]
        return {"id": cid, "status": status, "detail": detail}
    kind, value, age = cmd.get("kind"), cmd.get("value"), cmd.get("age_s")
    source = sanitize_source(cmd.get("source"))
    text = f"/{kind} {value} (web) {source}".strip()
    status, detail = "done", f"đã chuyển cho controller (≤ {cfg.control.loop_s} s)"
    if (kind, value) not in {("mode", "auto"), ("mode", "manual"), ("fan", "on"),
                             ("fan", "off"), ("buzzer", "on"), ("buzzer", "off")}:
        status, detail = "rejected", "lệnh không hợp lệ"
    elif not isinstance(age, (int, float)) or isinstance(age, bool) \
            or age > cfg.cloud.command_max_age_s or age < 0:
        status, detail = "rejected", "lệnh quá cũ"
    elif kind == "buzzer" and not cfg.buzzer.enabled:
        status, detail = "rejected", "còi đang tắt trong config.yaml"
    done[cid] = [status, detail]
    trimmed = dict(list(done.items())[-DONE_KEEP:])
    try:
        if status == "done":
            cmd_id = f"web:{cid}"
            if kind == "fan":
                commands.set_fan(conn, cfg, now, value == "on", cmd_id=cmd_id, detail=text, commit=False)
            elif kind == "buzzer":
                commands.set_buzzer(conn, cfg, now, value == "on", cmd_id=cmd_id, detail=text,
                                    commit=False)
            else:
                commands.set_mode(conn, cfg, now, value, cmd_id=cmd_id, detail=text, commit=False)
        else:
            from aiot.db import log_event
            log_event(conn, now, "command", f"{text} dropped: {detail}", commit=False)
        set_state(conn, DONE_KEY, json.dumps(trimmed), commit=False)
        conn.commit()
    except BaseException:
        conn.rollback()
        done.pop(cid, None)
        raise
    done.clear()
    done.update(trimmed)
    log.info("Lệnh web %s %s: %s (%s)", kind, value, status, detail)
    return {"id": cid, "status": status, "detail": detail}


def sanitize_source(source):
    if not isinstance(source, str):
        return "web"
    return re.sub(r"[^\w .:=\-]", "", source)[:80]


# ---------------- một lần gửi ----------------
class Uplink:
    def __init__(self, conn, cfg, session=None):
        if session is None:
            import requests
            session = requests.Session()
        self.conn = conn
        self.cfg = cfg
        self.session = session
        self.acks = []
        self.url = cfg.cloud.url.rstrip("/") + "/api/ingest"
        self.headers = {"Authorization": f"Bearer {cfg.secrets.ingest_token}"}

    def once(self, now=None):
        """Một lần gửi. True nếu cloud nhận (200). Lỗi mạng/HTTP thì False, không raise."""
        now = int(now if now is not None else time.time())
        payload, new_cur = build_payload(self.conn, self.cfg, now, self.acks)
        resp = None
        for attempt in (1, 2):
            try:
                resp = self.session.post(self.url, json=payload, headers=self.headers,
                                         timeout=self.cfg.cloud.timeout_s)
                break
            except Exception as e:      # requests.RequestException và lỗi mạng khác
                # Lần 1 lỗi kết nối: thường do server vừa đóng kết nối keep-alive đang rảnh
                # mà Session định dùng lại; thử lại ngay một lần bằng kết nối mới
                if attempt == 1 and type(e).__name__ == "ConnectionError":
                    continue
                log.warning("Không gửi được lên cloud: %s", type(e).__name__)
                return False
        if resp.status_code != 200:
            log.warning("Cloud trả HTTP %s", resp.status_code)
            return False
        try:
            body = resp.json()
        except ValueError:
            log.warning("Cloud trả về không phải JSON")
            return False
        set_state(self.conn, CURSOR_KEY, json.dumps(new_cur), commit=False)
        set_state(self.conn, LAST_OK_KEY, now, commit=False)
        self.conn.commit()
        self.acks = []
        cmds = body.get("commands") if isinstance(body, dict) else None
        if isinstance(cmds, list) and cmds:
            done = load_done(self.conn)
            for cmd in cmds[:20]:
                try:
                    ack = apply_command(self.conn, self.cfg, now, cmd, done)
                except Exception:
                    log.exception("Lỗi khi áp dụng lệnh web")
                    continue
                if ack is not None:
                    self.acks.append(ack)
        return True


def run(uplink, stop, *, wall=time.time):
    delay = uplink.cfg.cloud.interval_s
    while not stop.is_set():
        try:
            ok = uplink.once(int(wall()))
        except Exception:
            log.exception("Lỗi trong vòng uplink")
            try:
                uplink.conn.rollback()
            except Exception:
                pass
            ok = False
        # Có lệnh vừa áp dụng: gửi kết quả sớm để web hiện "xong" nhanh
        base = uplink.cfg.cloud.interval_s
        delay = base if ok else min(BACKOFF_MAX_S, max(base, delay * 2))
        stop.wait(1 if (ok and uplink.acks) else delay)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    from aiot.config import load_config

    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Đẩy dữ liệu trạm lên web trên cloud, nhận lệnh từ web")
    parser.add_argument("--config", default=str(root / "config.yaml"))
    args = parser.parse_args(argv)
    cfg = load_config(args.config, root / ".env")
    if not cfg.cloud.enabled or not cfg.cloud.url.strip():
        log.error("cloud.enabled false hoặc thiếu cloud.url trong config.yaml")
        return 2
    if not cfg.secrets.ingest_token:
        log.error("Thiếu INGEST_TOKEN trong .env")
        return 2
    if not cfg.cloud.url.startswith("https://") and "localhost" not in cfg.cloud.url \
            and "127.0.0.1" not in cfg.cloud.url:
        log.error("cloud.url phải là https:// (token không được gửi qua HTTP thường)")
        return 2

    stop = threading.Event()

    def on_signal(signum, frame):
        log.info("Nhận tín hiệu %s, đang dừng", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    conn = connect(cfg.db_path)
    init_schema(conn)
    log.info("Uplink bắt đầu: %s mỗi %ss", cfg.cloud.url, cfg.cloud.interval_s)
    try:
        run(Uplink(conn, cfg), stop)
    finally:
        conn.close()
        log.info("Uplink đã dừng")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
