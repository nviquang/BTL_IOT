"""
Luật bật/tắt quạt lọc (xem 'Logic điều khiển quạt' trong CLAUDE.md).

Toàn bộ là hàm thuần: không đọc đồng hồ, không I/O, không ngẫu nhiên.
Thời gian hiện tại luôn đến từ inp.now (epoch giây). Dịch vụ controller
(Task 6) đọc dữ liệu, gọi decide(), lưu ControlState và bật relay theo fan_on().

Hai trạng thái:
- NORMAL  (BÌNH THƯỜNG): quạt tắt
- PROTECT (BẢO VỆ):      quạt bật

Gemini và Internet không tham gia quyết định.
"""
import dataclasses
import json
import statistics
from dataclasses import dataclass

MODES = ("NORMAL", "PROTECT")


# ---------- tổng hợp chuỗi PM2.5 ----------

def _window(series, now, window_s):
    """Giá trị các mẫu có now - window_s < ts <= now. series: [(ts, giá trị)] tăng dần."""
    start = now - window_s
    return [v for ts, v in series if start < ts <= now]


def rolling_mean(series, now, window_s, sample_s=10, min_coverage=0.5):
    """Trung bình trượt; None nếu có ít hơn min_coverage số mẫu kỳ vọng trong cửa sổ."""
    values = _window(series, now, window_s)
    if not values or len(values) < min_coverage * window_s / sample_s:
        return None
    return sum(values) / len(values)


def rolling_median(series, now, window_s, min_samples=3):
    """Trung vị trượt (chống gai đơn lẻ); None nếu ít hơn min_samples mẫu."""
    values = _window(series, now, window_s)
    if len(values) < min_samples:
        return None
    return statistics.median(values)


# ---------- dữ liệu vào/ra ----------

@dataclass(frozen=True)
class Inputs:
    now: int
    last_sample_ts: int | None = None       # thời điểm mẫu PM2.5 mới nhất
    danger_level: float | None = None       # trung vị 1 phút
    mean_enter: float | None = None         # TB enter_mean_s (10 phút)
    mean_exit: float | None = None          # TB exit_mean_s (15 phút)
    forecast_3h: float | None = None        # dự báo TB 3 giờ tới
    forecast_made_at: int | None = None


def inputs_from_series(series, now, cfg, forecast_3h=None, forecast_made_at=None):
    """Dựng Inputs từ chuỗi PM2.5 [(ts, µg/m³)] tăng dần theo các cửa sổ trong cfg."""
    return Inputs(
        now=now,
        last_sample_ts=series[-1][0] if series else None,
        danger_level=rolling_median(series, now, cfg.danger_window_s),
        mean_enter=rolling_mean(series, now, cfg.enter_mean_s),
        mean_exit=rolling_mean(series, now, cfg.exit_mean_s),
        forecast_3h=forecast_3h,
        forecast_made_at=forecast_made_at,
    )


@dataclass(frozen=True)
class Event:
    """kind: fan_on, fan_off, danger, sensor_fault, sensor_recovered, override_expired.
    Với fan_on/fan_off, detail là lý do: enter, danger, proactive, exit."""
    kind: str
    detail: str = ""


@dataclass(frozen=True)
class ControlState:
    mode: str = "NORMAL"
    entered_at: int | None = None       # lần chuyển NORMAL -> PROTECT gần nhất
    enter_since: int | None = None      # điều kiện vào bắt đầu đúng liên tục từ lúc này
    exit_since: int | None = None       # điều kiện thoát bắt đầu đúng liên tục từ lúc này
    fault_active: bool = False          # đang mất dữ liệu PM
    override_fan: bool | None = None    # ghi đè thủ công: True bật, False tắt, None không ghi đè
    override_until: int | None = None
    danger_active: bool = False         # đang ở mức khẩn cấp (để chỉ báo một lần)

    def to_json(self):
        return json.dumps(dataclasses.asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text):
        """Đọc lại trạng thái; JSON hỏng hoặc thiếu trường thì ValueError."""
        try:
            data = json.loads(text)
        except (TypeError, ValueError) as e:
            raise ValueError(f"Trạng thái điều khiển không phải JSON hợp lệ: {e}") from None
        if not isinstance(data, dict):
            raise ValueError("Trạng thái điều khiển phải là một object JSON")
        names = [f.name for f in dataclasses.fields(cls)]
        missing = [n for n in names if n not in data]
        if missing:
            raise ValueError(f"Trạng thái điều khiển thiếu trường: {', '.join(missing)}")

        def is_int(v):
            return isinstance(v, int) and not isinstance(v, bool)

        if data["mode"] not in MODES:
            raise ValueError(f"mode không hợp lệ: {data['mode']!r}")
        for n in ("entered_at", "enter_since", "exit_since", "override_until"):
            if data[n] is not None and not is_int(data[n]):
                raise ValueError(f"{n} phải là số nguyên hoặc null")
        for n in ("fault_active", "danger_active"):
            if not isinstance(data[n], bool):
                raise ValueError(f"{n} phải là true/false")
        if data["override_fan"] is not None and not isinstance(data["override_fan"], bool):
            raise ValueError("override_fan phải là true/false hoặc null")
        if data["override_fan"] is not None and data["override_until"] is None:
            # Ghi đè không có hạn sẽ thành vĩnh viễn: không chấp nhận
            raise ValueError("override_fan được đặt thì override_until phải có giá trị")
        return cls(**{n: data[n] for n in names})


# ---------- ghi đè thủ công ----------

def override_active(st, now):
    """Ghi đè còn hiệu lực. Thiếu override_until coi như không ghi đè (không cho vĩnh viễn)."""
    return (st.override_fan is not None and st.override_until is not None
            and now < st.override_until)


def apply_override(st, fan, now, ttl_s):
    """/fan on|off: fan True/False, hết hạn sau ttl_s. fan None: xoá ghi đè (/mode auto)."""
    if fan is None:
        return dataclasses.replace(st, override_fan=None, override_until=None)
    return dataclasses.replace(st, override_fan=bool(fan), override_until=int(now) + int(ttl_s))


def fan_on(st, now):
    """Quạt có nên quay lúc now: ghi đè còn hạn thắng, ngược lại theo mode."""
    if override_active(st, now):
        return st.override_fan
    return st.mode == "PROTECT"


# ---------- máy trạng thái ----------

def _since(prev, condition, now):
    """Mốc bắt đầu của điều kiện đang đúng liên tục; điều kiện sai thì xoá."""
    if not condition:
        return None
    return prev if prev is not None else now


def decide(st, inp, cfg):
    """Một bước điều khiển. Trả về (trạng thái mới, danh sách Event)."""
    now = int(inp.now)              # mốc thời gian lưu lại luôn là số nguyên
    events = []

    # Ghi đè hết hạn: xoá và báo một lần. Máy trạng thái vẫn chạy bên dưới ghi đè.
    if st.override_fan is not None and st.override_until is not None and now >= st.override_until:
        st = dataclasses.replace(st, override_fan=None, override_until=None)
        events.append(Event("override_expired"))

    # Mất dữ liệu PM: giữ nguyên mode, xoá bộ đếm, không quyết định theo số liệu cũ.
    stale = inp.last_sample_ts is None or now - inp.last_sample_ts > cfg.stale_s
    if stale:
        if not st.fault_active:
            events.append(Event("sensor_fault"))
        st = dataclasses.replace(st, fault_active=True, enter_since=None,
                                 exit_since=None, danger_active=False)
        return st, events

    if st.fault_active:
        # Có lại dữ liệu: bộ đếm đã bị xoá nên phải chờ đủ thời gian từ đầu.
        events.append(Event("sensor_recovered"))
        st = dataclasses.replace(st, fault_active=False)

    # Mức khẩn cấp theo trung vị 1 phút; chỉ báo khi vừa chuyển sang khẩn cấp.
    danger = inp.danger_level is not None and inp.danger_level >= cfg.danger_pm25
    if danger and not st.danger_active:
        events.append(Event("danger"))
    st = dataclasses.replace(st, danger_active=danger)

    forecast_fresh = (inp.forecast_made_at is not None
                      and now - inp.forecast_made_at <= cfg.forecast_max_age_s)
    forecast_high = (forecast_fresh and inp.forecast_3h is not None
                     and inp.forecast_3h > cfg.proactive_pm25)

    if st.mode == "NORMAL":
        enter_since = _since(st.enter_since,
                             inp.mean_enter is not None and inp.mean_enter > cfg.enter_pm25, now)
        if danger:
            reason = "danger"
        elif forecast_high:
            reason = "proactive"
        elif enter_since is not None and now - enter_since >= cfg.enter_hold_s:
            reason = "enter"
        else:
            reason = None
        if reason:
            events.append(Event("fan_on", reason))
            st = dataclasses.replace(st, mode="PROTECT", entered_at=now,
                                     enter_since=None, exit_since=None)
        else:
            st = dataclasses.replace(st, enter_since=enter_since, exit_since=None)
        return st, events

    # PROTECT
    exit_since = _since(st.exit_since,
                        inp.mean_exit is not None and inp.mean_exit < cfg.exit_pm25, now)
    held_long_enough = st.entered_at is None or now - st.entered_at >= cfg.min_state_s
    if (exit_since is not None and now - exit_since >= cfg.exit_hold_s
            and held_long_enough and not forecast_high and not danger):
        events.append(Event("fan_off", "exit"))
        st = dataclasses.replace(st, mode="NORMAL", enter_since=None, exit_since=None)
    else:
        st = dataclasses.replace(st, exit_since=exit_since, enter_since=None)
    return st, events
