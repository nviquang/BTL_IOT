"""
Dịch vụ controller: mỗi loop_s giây đọc PM2.5 từ SQLite, chạy luật decide()
và bật/tắt quạt qua relay GPIO23.

- Chỉ dịch vụ này chạm GPIO23. Bot ghi khoá state 'pending_override' dạng JSON:
  {"fan": true|false, "ttl_s": int} đặt ghi đè; {"fan": null} xoá ghi đè (/mode auto).
  Controller đọc khoá ở đầu mỗi bước, áp dụng, ghi sự kiện 'override' rồi xoá khoá.
- Trạng thái logic (ControlState) lưu ở khoá 'control_state' sau mỗi bước, trước khi
  ghi relay. Relay được ghi lại mỗi vòng (idempotent), nên relay lỗi thì vòng sau tự
  thử lại; sự kiện 'relay_error' chỉ ghi một lần cho cả chuỗi lỗi.
- Khởi động: khôi phục trạng thái cuối trong DB (thiếu: BÌNH THƯỜNG; hỏng: BÌNH THƯỜNG
  và ghi 'state_corrupt'; ghi đè hết hạn lúc Pi tắt thì xoá) rồi đưa quạt về đúng mức.
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

from aiot.control import ControlState, apply_override, decide, fan_on, inputs_from_series
from aiot.db import connect, delete_state, get_state, init_schema, log_event, pm25_series, set_state

log = logging.getLogger("aiot.controller")

STATE_KEY = "control_state"
PENDING_KEY = "pending_override"
RELAY_FAULT_KEY = "relay_fault"     # "1" khi đang trong chuỗi lỗi relay


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---------------- Đọc/ghi trạng thái ----------------
def _load_state(conn, now: int) -> ControlState:
    """Trạng thái đã lưu; thiếu thì mặc định, hỏng thì mặc định và ghi 'state_corrupt'."""
    text = get_state(conn, STATE_KEY)
    if text is None:
        return ControlState()
    try:
        return ControlState.from_json(text)
    except ValueError as e:
        log.error("Trạng thái điều khiển hỏng, về BÌNH THƯỜNG: %s", e)
        log_event(conn, now, "state_corrupt", str(e))
        return ControlState()


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


def _take_pending_override(conn, st: ControlState, cfg, now: int) -> ControlState:
    """Áp dụng rồi xoá lệnh ghi đè bot để lại (nếu có)."""
    text = get_state(conn, PENDING_KEY)
    if text is None:
        return st
    try:
        fan, ttl_s = _parse_pending(text, cfg.override_ttl_s)
    except ValueError as e:
        log.warning("Bỏ qua pending_override sai định dạng: %s", e)
        log_event(conn, now, "override_invalid", f"{e}: {text[:200]}")
        delete_state(conn, PENDING_KEY)
        return st
    st = apply_override(st, fan, now, ttl_s)
    if fan is None:
        detail = "cleared (auto)"
    else:
        detail = (f"set fan={'on' if fan else 'off'} until={st.override_until} "
                  f"({_fmt_ts(st.override_until)})")
    log.info("Ghi đè thủ công: %s", detail)
    log_event(conn, now, "override", detail)
    delete_state(conn, PENDING_KEY)
    return st


def _drive(conn, relay, on: bool, now: int) -> bool:
    """Ghi mức relay. Lỗi thì ghi 'relay_error' một lần mỗi chuỗi lỗi, không raise."""
    try:
        relay.set(on)
    except Exception as e:
        log.error("Không ghi được relay (%s): %s", "bật" if on else "tắt", e)
        if get_state(conn, RELAY_FAULT_KEY) != "1":
            log_event(conn, now, "relay_error", f"set {'on' if on else 'off'}: {e}")
            set_state(conn, RELAY_FAULT_KEY, "1")
        return False
    if get_state(conn, RELAY_FAULT_KEY) == "1":
        log.info("Relay hoạt động lại")
        log_event(conn, now, "relay_recovered", f"set {'on' if on else 'off'}")
        delete_state(conn, RELAY_FAULT_KEY)
    return True


# ---------------- Khôi phục và một bước ----------------
def restore_state(conn, now: int) -> ControlState:
    """Trạng thái sau khởi động lại (đã lưu lại DB), chưa chạm relay."""
    now = int(now)
    st = _load_state(conn, now)
    if st.override_fan is not None and (st.override_until is None or now >= st.override_until):
        log.info("Ghi đè đã hết hạn trong lúc dịch vụ dừng, về tự động")
        st = apply_override(st, None, now, 0)
        log_event(conn, now, "override_expired", "during downtime")
    set_state(conn, STATE_KEY, st.to_json())
    return st


def restore(conn, relay, cfg, now: int) -> ControlState:
    """Khôi phục trạng thái rồi đưa relay về đúng mức ngay."""
    st = restore_state(conn, now)
    _drive(conn, relay, fan_on(st, int(now)), int(now))
    return st


def step(conn, relay, cfg, now: int) -> ControlState:
    """Một vòng điều khiển. cfg là ControlCfg, now là giây epoch."""
    now = int(now)
    st = _load_state(conn, now)
    st = _take_pending_override(conn, st, cfg, now)

    lookback = max(cfg.enter_mean_s, cfg.exit_mean_s, cfg.danger_window_s) + 60
    series = pm25_series(conn, now - lookback)
    # Task 9 sẽ truyền thêm dự báo 3 giờ tới
    st, events = decide(st, inputs_from_series(series, now, cfg), cfg)

    # Lưu trạng thái logic trước khi ghi relay: relay lỗi không làm mất quyết định
    set_state(conn, STATE_KEY, st.to_json())
    for ev in events:
        log.info("Sự kiện %s %s", ev.kind, ev.detail)
        log_event(conn, now, ev.kind, ev.detail)

    _drive(conn, relay, fan_on(st, now), now)
    return st


# ---------------- Vòng lặp dịch vụ ----------------
def run(conn, relay, cfg, stop: threading.Event, *, wall=time.time, clock=time.monotonic) -> None:
    """Gọi step mỗi cfg.loop_s giây tới khi stop được đặt. Lỗi một vòng không dừng dịch vụ;
    khi đó relay giữ nguyên mức cũ."""
    next_time = clock()
    while not stop.is_set():
        try:
            step(conn, relay, cfg, int(wall()))
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
        run(conn, relay, ctrl, stop)
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
