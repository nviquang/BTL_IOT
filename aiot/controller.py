"""
Dịch vụ controller: mỗi loop_s giây đọc PM2.5 từ SQLite, chạy luật decide()
và bật/tắt quạt qua relay GPIO23.

- Chỉ dịch vụ này chạm GPIO23. Bot ghi khoá state 'pending_override' dạng JSON:
  {"fan": true|false, "ttl_s": int, "at": epoch} đặt ghi đè; {"fan": null, "at": epoch}
  xoá ghi đè (/mode auto). Controller đọc khoá ở đầu mỗi bước; trạng thái mới, sự kiện
  'override' và lệnh xoá khoá (chỉ khi giá trị chưa đổi) được ghi trong cùng một transaction.
  Lệnh thiếu "at", hoặc lệch quá telegram.stale_command_s + control.loop_s so với giờ hiện
  tại (cũ, hay "ở tương lai"), bị bỏ với sự kiện 'override_stale': controller dừng vài giờ
  thì lệnh /fan cũ không được chạy khi nó chạy lại.
- Mỗi bước ghi khoá 'controller_heartbeat' (giây epoch) cùng transaction; bot dùng khoá
  này để cảnh báo khi controller không chạy.
- Trạng thái logic (ControlState) lưu ở khoá 'control_state' sau mỗi bước, trước khi
  ghi relay. Relay được ghi lại mỗi vòng (idempotent), nên relay lỗi thì vòng sau tự
  thử lại; sự kiện 'relay_error' chỉ ghi một lần cho cả chuỗi lỗi.
- Khởi động: khôi phục trạng thái cuối trong DB (thiếu: BÌNH THƯỜNG; hỏng: BÌNH THƯỜNG
  và ghi 'state_corrupt'; ghi đè hết hạn lúc Pi tắt thì xoá; xoá mốc enter_since/exit_since
  vì điều kiện không được theo dõi trong lúc dừng) rồi đưa quạt về đúng mức.
- Sau khi Pi khởi động lại: chỉ dùng mẫu thô có ts >= boot_wall = now - time.monotonic()
  (mẫu trước khi tắt máy không lọt vào cửa sổ 10/15 phút khi đồng hồ được khôi phục gần
  giờ tắt). Trong stale_s giây đầu sau khi khởi động, thiếu dữ liệu PM vẫn giữ nguyên trạng
  thái nhưng không báo 'sensor_fault' (collector chưa kịp ghi mẫu đầu); quá thời gian đó mà
  vẫn thiếu thì báo như thường.
- Relay lỗi: ngoài sự kiện 'relay_error', lần lỗi đầu của chuỗi gửi cảnh báo 'relay_error'
  (theo giới hạn tin của notifier); tin fan_on/fan_off của cùng bước được sửa để không
  khẳng định quạt đã đổi trạng thái.
- Cảnh báo (Task 7): Event của bước (danger, fan_on/off, sensor_fault...) và đổi nhóm
  AQI theo PM2.5 TB exit_mean_s (15 phút) được ghi vào outbox trong CÙNG transaction với
  trạng thái và sự kiện, đặt sau SAVEPOINT riêng: lỗi trong phần tin nhắn chỉ huỷ phần
  tin nhắn, ghi log rồi tiếp tục; trạng thái, sự kiện và relay không bị ảnh hưởng.
- Dự báo (Task 9): mỗi bước đọc latest_forecast (TB 3 giờ tới của dự báo mới nhất, bỏ
  qua nếu cũ hơn control.forecast_max_age_s) cho luật 'Chủ động'. Đọc lỗi thì coi như
  không có dự báo, ghi log một lần, vẫn điều khiển bình thường.
- SIGTERM/SIGINT: đưa GPIO23 về LOW, nhả chip, thoát 0.
- Không phụ thuộc Internet hay Gemini.

Chạy: cd ~/BTL_IOT && venv/bin/python -m aiot.controller [--config demo.yaml]
"""
import argparse
import dataclasses
import json
import logging
import signal
import threading
import time
from datetime import datetime
from pathlib import Path

from aiot.config import TelegramCfg
from aiot.control import ControlState, Event, apply_override, decide, fan_on, inputs_from_series
from aiot.db import connect, delete_state, get_state, init_schema, log_event, pm25_series, set_state
from aiot.forecast import latest_forecast
from aiot.notifier import (FAULT_PENDING_KEY, check_aqi_change, event_message, resend_suppressed,
                           send_alert)

log = logging.getLogger("aiot.controller")

STATE_KEY = "control_state"
PENDING_KEY = "pending_override"
RELAY_FAULT_KEY = "relay_fault"     # "1" khi đang trong chuỗi lỗi relay
HEARTBEAT_KEY = "controller_heartbeat"   # giây epoch của bước gần nhất (bot đọc)
RELAY_ERROR_TEXT = ("Lỗi relay: bộ điều khiển không ghi được GPIO23 để {action} quạt lọc, "
                    "quạt có thể không phản hồi. Kiểm tra dây relay và dịch vụ aiot-controller.")
RELAY_FAN_NOTE = " (Lưu ý: không điều khiển được relay, quạt có thể chưa {action}.)"

_forecast_error_logged = False      # lỗi đọc dự báo chỉ ghi log một lần mỗi chuỗi lỗi


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---------------- Đọc/ghi trạng thái ----------------
def _load_state(conn):
    """(trạng thái đã lưu, danh sách Event). Thiếu thì mặc định; hỏng thì mặc định
    kèm Event 'state_corrupt'. Không ghi gì: người gọi ghi cùng transaction của bước."""
    text = get_state(conn, STATE_KEY)
    if text is None:
        return ControlState(), []
    try:
        return ControlState.from_json(text), []
    except ValueError as e:
        log.error("Trạng thái điều khiển hỏng, về BÌNH THƯỜNG: %s", e)
        return ControlState(), [Event("state_corrupt", str(e))]


def _parse_pending(text, default_ttl_s: int):
    """(fan, ttl_s, at) từ JSON pending_override; sai định dạng thì ValueError.
    at (giây epoch lúc bot nhận lệnh) là None khi thiếu hoặc không phải số nguyên."""
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("không phải JSON") from None
    if not isinstance(data, dict) or "fan" not in data:
        raise ValueError("thiếu khoá 'fan'")
    fan = data["fan"]
    if fan is not None and not isinstance(fan, bool):
        raise ValueError("'fan' phải là true/false/null")
    ttl_s = data.get("ttl_s", default_ttl_s)
    if fan is not None and (not isinstance(ttl_s, int) or isinstance(ttl_s, bool) or ttl_s <= 0):
        raise ValueError("'ttl_s' phải là số nguyên dương")
    at = data.get("at")
    if not isinstance(at, int) or isinstance(at, bool):
        at = None
    return fan, ttl_s, at


def _apply_pending(st: ControlState, text: str, cfg, now: int, max_age_s: int):
    """Áp dụng lệnh ghi đè bot để lại vào st (trong bộ nhớ). Trả về (st, [Event]).
    Lệnh thiếu "at" hoặc lệch quá max_age_s giây so với now bị bỏ ('override_stale')."""
    try:
        fan, ttl_s, at = _parse_pending(text, cfg.override_ttl_s)
    except ValueError as e:
        log.warning("Bỏ qua pending_override sai định dạng: %s", e)
        return st, [Event("override_invalid", f"{e}: {text[:200]}")]
    if at is None or abs(now - at) > max_age_s:
        age = "no 'at'" if at is None else f"at={at} now={now} max_age_s={max_age_s}"
        log.warning("Bỏ lệnh ghi đè cũ hoặc sai giờ (%s)", age)
        return st, [Event("override_stale", f"{age}: {text[:200]}")]
    st = apply_override(st, fan, now, ttl_s)
    if fan is None:
        detail = "cleared (auto)"
    else:
        detail = (f"set fan={'on' if fan else 'off'} until={st.override_until} "
                  f"({_fmt_ts(st.override_until)})")
    log.info("Ghi đè thủ công: %s", detail)
    return st, [Event("override", detail)]


def _notify(conn, now: int, st: ControlState, inp, events, tcfg) -> None:
    """Ghi tin cảnh báo của bước vào outbox (không commit: nằm trong transaction của _persist)."""
    for ev in events:
        msg = event_message(ev, st, inp, now)
        if msg is None:
            continue
        if ev.kind == "sensor_recovered" and get_state(conn, FAULT_PENDING_KEY) is not None:
            continue                    # lỗi chưa từng được báo thì không báo đã hết lỗi
        text, urgent = msg
        send_alert(conn, ev.kind, text, now, tcfg, urgent=urgent, commit=False)
    resend_suppressed(conn, now, tcfg, st.fault_active, commit=False)
    if inp is not None and not st.fault_active:
        # Mất dữ liệu: TB 15 phút có thể vẫn tính được từ mẫu cũ, không báo đổi nhóm theo nó
        check_aqi_change(conn, inp.mean_exit, now, tcfg, commit=False)


def _run_notifier(conn, notify) -> None:
    """Chạy notify() sau SAVEPOINT: lỗi thì huỷ riêng phần tin nhắn, ghi log, không raise."""
    conn.execute("SAVEPOINT notify")
    try:
        notify()
    except Exception:
        log.exception("Lỗi khi tạo tin cảnh báo, bỏ qua tin của vòng này")
        conn.execute("ROLLBACK TO SAVEPOINT notify")
    conn.execute("RELEASE SAVEPOINT notify")


def _persist(conn, now: int, st: ControlState, events, pending_text=None, notify=None) -> None:
    """Ghi control_state, mọi Event, (nếu có) xoá đúng lệnh pending đã đọc và tin cảnh báo
    (notify) trong MỘT transaction, commit một lần. Lỗi ghi DB thì rollback toàn bộ và
    raise: lệnh pending còn nguyên cho vòng sau, không có sự kiện nào được ghi mà không
    áp dụng. Lỗi trong notify chỉ huỷ phần tin nhắn (_run_notifier)."""
    try:
        set_state(conn, STATE_KEY, st.to_json(), commit=False)
        set_state(conn, HEARTBEAT_KEY, int(now), commit=False)
        for ev in events:
            log.info("Sự kiện %s %s", ev.kind, ev.detail)
            log_event(conn, now, ev.kind, ev.detail, commit=False)
        if pending_text is not None:
            # Chỉ xoá nếu bot chưa ghi lệnh mới kể từ lúc đọc
            delete_state(conn, PENDING_KEY, value=pending_text, commit=False)
        if notify is not None:
            _run_notifier(conn, notify)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _drive(conn, relay, on: bool, now: int, tcfg: TelegramCfg | None = None) -> bool:
    """Ghi mức relay. Lỗi thì ghi 'relay_error' và gửi cảnh báo một lần mỗi chuỗi lỗi
    (cảnh báo theo giới hạn tin của notifier), không raise."""
    tcfg = tcfg if tcfg is not None else TelegramCfg()
    try:
        relay.set(on)
    except Exception as e:
        log.error("Không ghi được relay (%s): %s", "bật" if on else "tắt", e)
        if get_state(conn, RELAY_FAULT_KEY) != "1":
            try:
                log_event(conn, now, "relay_error", f"set {'on' if on else 'off'}: {e}", commit=False)
                set_state(conn, RELAY_FAULT_KEY, "1", commit=False)
                _run_notifier(conn, lambda: send_alert(
                    conn, "relay_error", RELAY_ERROR_TEXT.format(action="bật" if on else "tắt"),
                    now, tcfg, commit=False))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return False
    if get_state(conn, RELAY_FAULT_KEY) == "1":
        log.info("Relay hoạt động lại")
        try:
            log_event(conn, now, "relay_recovered", f"set {'on' if on else 'off'}", commit=False)
            delete_state(conn, RELAY_FAULT_KEY, commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return True


def _read_forecast(conn, cfg, now: int):
    """(TB 3 giờ tới, made_at) hoặc (None, None). Không bao giờ raise."""
    global _forecast_error_logged
    try:
        found = latest_forecast(conn, now, cfg.forecast_max_age_s)
    except Exception as e:
        if not _forecast_error_logged:
            log.warning("Không đọc được dự báo, điều khiển không có dự báo: %s", e)
            _forecast_error_logged = True
        return None, None
    if _forecast_error_logged:
        log.info("Đọc được dự báo trở lại")
        _forecast_error_logged = False
    return found if found is not None else (None, None)


# ---------------- Khôi phục và một bước ----------------
def restore_state(conn, now: int) -> ControlState:
    """Trạng thái sau khởi động lại (đã lưu lại DB), chưa chạm relay.

    Xoá enter_since/exit_since: trong lúc dừng không ai theo dõi điều kiện, nên mốc cũ
    không còn nghĩa "đúng liên tục"; phải chờ đủ thời gian từ đầu. mode và entered_at giữ."""
    now = int(now)
    st, events = _load_state(conn)
    st = dataclasses.replace(st, enter_since=None, exit_since=None)
    if st.override_fan is not None and (st.override_until is None or now >= st.override_until):
        log.info("Ghi đè đã hết hạn trong lúc dịch vụ dừng, về tự động")
        st = apply_override(st, None, now, 0)
        events.append(Event("override_expired", "during downtime"))
    _persist(conn, now, st, events)
    return st


def restore(conn, relay, cfg, now: int, *, tcfg: TelegramCfg | None = None) -> ControlState:
    """Khôi phục trạng thái rồi đưa relay về đúng mức ngay."""
    st = restore_state(conn, now)
    _drive(conn, relay, fan_on(st, int(now)), int(now), tcfg)
    return st


def _flag_unconfirmed_fan_messages(conn, now: int, events, on: bool) -> None:
    """Relay lỗi ngay trong bước vừa báo fan_on/fan_off: sửa tin chưa gửi của bước đó để
    không khẳng định quạt đã đổi trạng thái. Lỗi ở đây chỉ ghi log."""
    kinds = [ev.kind for ev in events if ev.kind in ("fan_on", "fan_off")]
    if not kinds:
        return
    note = RELAY_FAN_NOTE.format(action="bật" if on else "tắt")
    try:
        conn.executemany(
            "UPDATE outbox SET text = text || ? WHERE created_at = ? AND kind = ? "
            "AND sent_at IS NULL", [(note, int(now), k) for k in kinds])
        conn.commit()
    except Exception as e:
        log.error("Không sửa được tin quạt sau lỗi relay: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass


def step(conn, relay, cfg, now: int, *, tcfg: TelegramCfg | None = None,
         monotonic=time.monotonic) -> ControlState:
    """Một vòng điều khiển. cfg là ControlCfg, tcfg là TelegramCfg (giờ yên lặng, giới hạn
    tin, tuổi lệnh; mặc định TelegramCfg()), now là giây epoch, monotonic() là số giây từ
    lúc Pi khởi động (time.monotonic trên Linux).

    Đọc hết rồi mới ghi: trạng thái mới, sự kiện, việc xoá pending_override và tin cảnh
    báo cùng một transaction (_persist), sau đó mới ghi relay."""
    now = int(now)
    tcfg = tcfg if tcfg is not None else TelegramCfg()
    st, events = _load_state(conn)
    pending_text = get_state(conn, PENDING_KEY)
    if pending_text is not None:
        max_age_s = tcfg.stale_command_s + cfg.loop_s
        st, pending_events = _apply_pending(st, pending_text, cfg, now, max_age_s)
        events += pending_events

    uptime = monotonic()
    boot_wall = int(now - uptime)       # giờ khởi động theo đồng hồ hiện tại
    lookback = max(cfg.enter_mean_s, cfg.exit_mean_s, cfg.danger_window_s) + 60
    series = pm25_series(conn, max(now - lookback, boot_wall))
    forecast_3h, forecast_made_at = _read_forecast(conn, cfg, now)
    inp = inputs_from_series(series, now, cfg, forecast_3h=forecast_3h,
                             forecast_made_at=forecast_made_at)
    fault_before = st.fault_active
    st, decide_events = decide(st, inp, cfg)
    if uptime < cfg.stale_s and any(ev.kind == "sensor_fault" for ev in decide_events):
        # Vừa khởi động, collector chưa kịp ghi mẫu đầu: vẫn giữ nguyên trạng thái (decide
        # đã xoá bộ đếm) nhưng không báo lỗi; hết stale_s mà vẫn thiếu thì decide báo lại
        decide_events = [ev for ev in decide_events if ev.kind != "sensor_fault"]
        st = dataclasses.replace(st, fault_active=fault_before)
    events += decide_events

    # Lưu trạng thái logic trước khi ghi relay: relay lỗi không làm mất quyết định
    _persist(conn, now, st, events, pending_text,
             notify=lambda: _notify(conn, now, st, inp, events, tcfg))
    level = fan_on(st, now)
    if not _drive(conn, relay, level, now, tcfg):
        _flag_unconfirmed_fan_messages(conn, now, events, level)
    return st


# ---------------- Vòng lặp dịch vụ ----------------
def run(conn, relay, cfg, stop: threading.Event, *, wall=time.time, clock=time.monotonic,
        tcfg: TelegramCfg | None = None) -> None:
    """Gọi step mỗi cfg.loop_s giây tới khi stop được đặt. Lỗi một vòng không dừng dịch vụ;
    khi đó relay giữ nguyên mức cũ."""
    next_time = clock()
    while not stop.is_set():
        try:
            step(conn, relay, cfg, int(wall()), tcfg=tcfg)
        except Exception:
            log.exception("Lỗi trong vòng điều khiển, giữ nguyên quạt")
            try:
                conn.rollback()
            except Exception as e:
                log.error("Rollback lỗi: %s", e)

        next_time += cfg.loop_s
        delay = next_time - clock()
        if delay > 0:
            stop.wait(delay)
        else:
            next_time = clock()


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from aiot.config import load_config
    from aiot.relay import Relay

    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Dịch vụ điều khiển quạt lọc qua relay GPIO23")
    parser.add_argument("--config", default=str(root / "config.yaml"),
                        help="file cấu hình (mặc định config.yaml trong thư mục repo)")
    args = parser.parse_args(argv)

    cfg = load_config(args.config, root / ".env")
    ctrl = cfg.control

    stop = threading.Event()

    def on_signal(signum, frame):
        log.info("Nhận tín hiệu %s, đang dừng", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    conn = connect(cfg.db_path)
    init_schema(conn)
    now = int(time.time())
    st = restore_state(conn, now)
    level = fan_on(st, now)
    # Nhận chân với mức đã khôi phục để quạt không tắt một nhịp khi khởi động lại
    relay = Relay(pin=23, initial=level)
    _drive(conn, relay, level, now, cfg.telegram)
    log.info("Controller bắt đầu: db=%s mode=%s quạt=%s demo=%s loop=%ss",
             cfg.db_path, st.mode, "bật" if level else "tắt", ctrl.demo, ctrl.loop_s)
    try:
        run(conn, relay, ctrl, stop, tcfg=cfg.telegram)
    finally:
        try:
            relay.close()               # GPIO23 về LOW, nhả chip
            log.info("Đã tắt quạt (GPIO23 LOW) và nhả GPIO")
        except Exception as e:
            log.error("Không tắt được relay khi dừng: %s", e)
        try:
            log_event(conn, int(time.time()), "controller_stop", "fan off")
        except Exception as e:
            log.error("Không ghi được sự kiện dừng: %s", e)
        conn.close()
        log.info("Controller đã dừng")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
