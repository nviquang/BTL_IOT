"""
Dịch vụ controller: mỗi loop_s giây đọc PM2.5 từ SQLite, chạy luật decide()
và bật/tắt quạt qua relay GPIO23.

- Chỉ dịch vụ này chạm GPIO23. Bot ghi khoá state 'pending_override' dạng JSON:
  {"fan": true|false, "ttl_s": int} đặt ghi đè; {"fan": null} xoá ghi đè (/mode auto).
  Controller đọc khoá ở đầu mỗi bước; trạng thái mới, sự kiện 'override' và lệnh xoá
  khoá (chỉ khi giá trị chưa đổi) được ghi trong cùng một transaction.
- Trạng thái logic (ControlState) lưu ở khoá 'control_state' sau mỗi bước, trước khi
  ghi relay. Relay được ghi lại mỗi vòng (idempotent), nên relay lỗi thì vòng sau tự
  thử lại; sự kiện 'relay_error' chỉ ghi một lần cho cả chuỗi lỗi.
- Khởi động: khôi phục trạng thái cuối trong DB (thiếu: BÌNH THƯỜNG; hỏng: BÌNH THƯỜNG
  và ghi 'state_corrupt'; ghi đè hết hạn lúc Pi tắt thì xoá) rồi đưa quạt về đúng mức.
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
    """(fan, ttl_s) từ JSON pending_override; sai định dạng thì ValueError."""
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
    return fan, ttl_s


def _apply_pending(st: ControlState, text: str, cfg, now: int):
    """Áp dụng lệnh ghi đè bot để lại vào st (trong bộ nhớ). Trả về (st, [Event])."""
    try:
        fan, ttl_s = _parse_pending(text, cfg.override_ttl_s)
    except ValueError as e:
        log.warning("Bỏ qua pending_override sai định dạng: %s", e)
        return st, [Event("override_invalid", f"{e}: {text[:200]}")]
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
    if inp is not None:
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


def _drive(conn, relay, on: bool, now: int) -> bool:
    """Ghi mức relay. Lỗi thì ghi 'relay_error' một lần mỗi chuỗi lỗi, không raise."""
    try:
        relay.set(on)
    except Exception as e:
        log.error("Không ghi được relay (%s): %s", "bật" if on else "tắt", e)
        if get_state(conn, RELAY_FAULT_KEY) != "1":
            try:
                log_event(conn, now, "relay_error", f"set {'on' if on else 'off'}: {e}", commit=False)
                set_state(conn, RELAY_FAULT_KEY, "1", commit=False)
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
    """Trạng thái sau khởi động lại (đã lưu lại DB), chưa chạm relay."""
    now = int(now)
    st, events = _load_state(conn)
    if st.override_fan is not None and (st.override_until is None or now >= st.override_until):
        log.info("Ghi đè đã hết hạn trong lúc dịch vụ dừng, về tự động")
        st = apply_override(st, None, now, 0)
        events.append(Event("override_expired", "during downtime"))
    _persist(conn, now, st, events)
    return st


def restore(conn, relay, cfg, now: int) -> ControlState:
    """Khôi phục trạng thái rồi đưa relay về đúng mức ngay."""
    st = restore_state(conn, now)
    _drive(conn, relay, fan_on(st, int(now)), int(now))
    return st


def step(conn, relay, cfg, now: int, *, tcfg: TelegramCfg | None = None) -> ControlState:
    """Một vòng điều khiển. cfg là ControlCfg, tcfg là TelegramCfg (giờ yên lặng, giới hạn
    tin; mặc định TelegramCfg()), now là giây epoch.

    Đọc hết rồi mới ghi: trạng thái mới, sự kiện, việc xoá pending_override và tin cảnh
    báo cùng một transaction (_persist), sau đó mới ghi relay."""
    now = int(now)
    tcfg = tcfg if tcfg is not None else TelegramCfg()
    st, events = _load_state(conn)
    pending_text = get_state(conn, PENDING_KEY)
    if pending_text is not None:
        st, pending_events = _apply_pending(st, pending_text, cfg, now)
        events += pending_events

    lookback = max(cfg.enter_mean_s, cfg.exit_mean_s, cfg.danger_window_s) + 60
    series = pm25_series(conn, now - lookback)
    forecast_3h, forecast_made_at = _read_forecast(conn, cfg, now)
    inp = inputs_from_series(series, now, cfg, forecast_3h=forecast_3h,
                             forecast_made_at=forecast_made_at)
    st, decide_events = decide(st, inp, cfg)
    events += decide_events

    # Lưu trạng thái logic trước khi ghi relay: relay lỗi không làm mất quyết định
    _persist(conn, now, st, events, pending_text,
             notify=lambda: _notify(conn, now, st, inp, events, tcfg))
    _drive(conn, relay, fan_on(st, now), now)
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
    _drive(conn, relay, level, now)
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
