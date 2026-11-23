"""
Luật còi báo vượt ngưỡng tại chỗ (chạy trong dịch vụ controller).

Toàn bộ là hàm thuần: không đọc đồng hồ, không I/O. Controller gọi alarm_reason()
và plan() mỗi vòng rồi ra lệnh cho aiot.buzzer.Buzzer theo hành động trả về.

- Có nhãn AI (aiot/source_model.py): kêu khi nhãn là ô nhiễm hoặc khói thuốc/nhang, không
  kêu khi nấu ăn hay bình thường. Không có nhãn AI: kêu khi trung vị PM2.5 1 phút
  >= buzzer.alarm_pm25 (luật cũ).
- Kêu một hồi (burst_s giây) ngay khi vừa vượt ngưỡng, lặp lại mỗi repeat_s giây nếu
  vẫn còn vượt; hết vượt ngưỡng thì tắt ngay.
- Mất dữ liệu PM: không có trung vị 1 phút nên không kêu theo PM (lỗi cảm biến đã có
  cảnh báo Telegram riêng).
- Đang tắt tiếng (/mute): không kêu, nhưng vẫn theo dõi để kêu lại khi hết tắt tiếng.
- Còi kêu bất kể giờ yên lặng: đây là cảnh báo an toàn tại chỗ, vẫn chạy khi mất Internet.
"""
import dataclasses
import json
from dataclasses import dataclass

REASONS = ("pm25", "pollution", "smoking")
AI_BUZZER_SOURCES = frozenset({"pollution", "smoking"})
BURST = "burst"
SILENCE = "silence"


@dataclass(frozen=True)
class AlarmState:
    reason: str | None = None           # lý do đang vượt ngưỡng: 'pm25' hoặc None
    last_burst_at: int | None = None    # lần còi kêu gần nhất (giây epoch)

    def to_json(self):
        return json.dumps(dataclasses.asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text):
        """Đọc lại trạng thái; thiếu hoặc hỏng thì trạng thái mặc định (không vượt ngưỡng)."""
        try:
            data = json.loads(text)
            reason = data["reason"]
            last = data["last_burst_at"]
        except (TypeError, ValueError, KeyError):
            return cls()
        if reason not in REASONS:
            reason = None
        if not isinstance(last, int) or isinstance(last, bool):
            last = None
        return cls(reason=reason, last_burst_at=last)


def alarm_reason(danger_level, cfg, source=None):
    """Lý do kêu còi lúc này, hoặc None. source: nhãn AI đã làm mượt, None = luật ngưỡng."""
    if source is not None:
        return source if source in AI_BUZZER_SOURCES else None
    if danger_level is not None and danger_level >= cfg.alarm_pm25:
        return "pm25"
    return None


def plan(st, reason, now, cfg, muted=False):
    """Một bước của còi. Trả về (trạng thái mới, hành động).

    Hành động: BURST (kêu một hồi), SILENCE (tắt còi) hoặc None (giữ nguyên)."""
    now = int(now)
    if reason is None:
        return AlarmState(), (SILENCE if st.reason is not None else None)
    if muted:
        return dataclasses.replace(st, reason=reason), SILENCE
    due = (st.reason != reason or st.last_burst_at is None
           or now - st.last_burst_at >= cfg.repeat_s)
    if due:
        return AlarmState(reason=reason, last_burst_at=now), BURST
    return st, None


def is_muted(value, now):
    """value: chuỗi giây epoch hết tắt tiếng (khoá state do bot ghi), hoặc None."""
    try:
        return value is not None and int(now) < int(value)
    except (TypeError, ValueError):
        return False
