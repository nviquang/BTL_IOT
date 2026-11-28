"""
Lệnh điều khiển từ web (qua aiot.uplink).

Không chạm GPIO: mỗi lệnh chỉ ghi khoá state cho controller, cùng một transaction với sự
kiện 'command' (detail = văn bản lệnh + nguồn, ví dụ "web user=x ip=y").

- 'pending_override' (quạt): {"fan": true|false, "ttl_s": int, "id": ..., "at": epoch} đặt
  ghi đè; {"fan": null, "id": ..., "at": epoch} xoá ghi đè (về tự động).
- 'pending_buzzer' (còi): {"on": true|false, "ttl_s": int, "id": ..., "at": epoch} đặt ghi
  đè; {"on": null, ...} xoá. Controller chuyển thành khoá 'buzzer_override'
  {"on": bool, "until": epoch, "at": epoch}. Ghi đè bật còi tối đa buzzer.manual_max_s.
- "id" làm hai lệnh giống nhau liên tiếp thành hai văn bản khác nhau (controller chỉ xoá
  khoá khi văn bản chưa đổi); "at" để controller bỏ lệnh đã chờ quá lâu.
- Chế độ thủ công = quạt hoặc còi đang có ghi đè. set_mode("auto") xoá cả hai.
"""
import json

from aiot.control import ControlState, fan_on, override_active
from aiot.db import get_state, log_event, set_state

PENDING_KEY = "pending_override"
BUZZER_PENDING_KEY = "pending_buzzer"
BUZZER_OVERRIDE_KEY = "buzzer_override"
STATE_KEY = "control_state"


def pending_fan_json(fan, cmd_id, now, ttl_s=None):
    data = {"fan": fan}
    if fan is not None:
        data["ttl_s"] = int(ttl_s)
    data["id"] = cmd_id
    data["at"] = int(now)
    return json.dumps(data, sort_keys=True)


def pending_buzzer_json(on, cmd_id, now, ttl_s=None):
    data = {"on": on}
    if on is not None:
        data["ttl_s"] = int(ttl_s)
    data["id"] = cmd_id
    data["at"] = int(now)
    return json.dumps(data, sort_keys=True)


def apply_writes(conn, now, writes, detail, *, commit=True):
    """Ghi các khoá state và sự kiện 'command' trong MỘT transaction.
    commit False: người gọi commit (gộp vào transaction của người gọi)."""
    try:
        for key, value in writes:
            set_state(conn, key, value, commit=False)
        log_event(conn, now, "command", detail[:300], commit=False)
        if commit:
            conn.commit()
    except BaseException:
        if commit:
            conn.rollback()
        raise


def control_state(conn):
    """ControlState đã lưu; thiếu hoặc hỏng thì None."""
    text = get_state(conn, STATE_KEY)
    if text is None:
        return ControlState()
    try:
        return ControlState.from_json(text)
    except ValueError:
        return None


def buzzer_ttl(cfg, on):
    if on:
        return max(1, min(cfg.control.override_ttl_s, cfg.buzzer.manual_max_s))
    return cfg.control.override_ttl_s


def set_fan(conn, cfg, now, on, *, cmd_id, detail, commit=True):
    """Ghi đè quạt bật/tắt. Trả về thời hạn ghi đè (giây)."""
    ttl = cfg.control.override_ttl_s
    apply_writes(conn, now, [(PENDING_KEY, pending_fan_json(bool(on), cmd_id, now, ttl))],
                 detail, commit=commit)
    return ttl


def set_buzzer(conn, cfg, now, on, *, cmd_id, detail, commit=True):
    """Ghi đè còi: bật (kêu liên tục, tối đa buzzer.manual_max_s) hoặc tắt (giữ im).
    Trả về thời hạn ghi đè (giây)."""
    ttl = buzzer_ttl(cfg, on)
    apply_writes(conn, now, [(BUZZER_PENDING_KEY, pending_buzzer_json(bool(on), cmd_id, now, ttl))],
                 detail, commit=commit)
    return ttl


def set_mode(conn, cfg, now, mode, *, cmd_id, detail, commit=True):
    """mode 'auto': xoá ghi đè quạt và còi, trả về None.
    mode 'manual': giữ quạt như hiện tại trong override_ttl_s, trả về mức quạt đang giữ."""
    if mode == "auto":
        apply_writes(conn, now, [(PENDING_KEY, pending_fan_json(None, cmd_id, now)),
                                 (BUZZER_PENDING_KEY, pending_buzzer_json(None, cmd_id, now))],
                     detail, commit=commit)
        return None
    if mode != "manual":
        raise ValueError(f"chế độ không hợp lệ: {mode!r}")
    st = control_state(conn)
    on = fan_on(st, now) if st is not None else False
    ttl = cfg.control.override_ttl_s
    apply_writes(conn, now, [(PENDING_KEY, pending_fan_json(on, cmd_id, now, ttl))],
                 detail, commit=commit)
    return on


def buzzer_override(conn, now):
    """(on, until, at) của ghi đè còi đang còn hạn, hoặc None."""
    try:
        data = json.loads(get_state(conn, BUZZER_OVERRIDE_KEY) or "null")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    on, until, at = data.get("on"), data.get("until"), data.get("at")
    if not isinstance(on, bool) or not isinstance(until, int) or isinstance(until, bool):
        return None
    if not isinstance(at, int) or isinstance(at, bool):
        at = 0
    if int(now) >= until:
        return None
    return on, until, at


def is_manual(conn, now):
    st = control_state(conn)
    fan_manual = st is not None and override_active(st, now)
    return fan_manual or buzzer_override(conn, now) is not None
