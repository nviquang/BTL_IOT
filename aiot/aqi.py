"""
Phân nhóm chất lượng không khí theo PM2.5 (US EPA 2024, µg/m³).

Tốt 0–9,0 · Trung bình 9,1–35,4 · Kém 35,5–55,4 · Xấu 55,5–125,4 ·
Rất xấu 125,5–225,4 · Nguy hại ≥ 225,5.

Giá trị được làm tròn tới 1 chữ số thập phân trước khi so, nên không có khoảng
trống giữa hai nhóm (ví dụ 9,04 là Tốt, 9,06 là Trung bình).
"""
import math
from typing import NamedTuple


class AqiLevel(NamedTuple):
    index: int      # 0 = Tốt ... 5 = Nguy hại
    label: str


# (cận trên bao gồm, nhãn); nhóm cuối không có cận trên
BANDS = (
    (9.0, "Tốt"),
    (35.4, "Trung bình"),
    (55.4, "Kém"),
    (125.4, "Xấu"),
    (225.4, "Rất xấu"),
    (math.inf, "Nguy hại"),
)
LABELS = tuple(label for _, label in BANDS)


def classify(pm25: float) -> AqiLevel:
    """Nhóm AQI của một giá trị PM2.5. None, NaN, âm hoặc vô cực âm thì ValueError."""
    if pm25 is None:
        raise ValueError("PM2.5 không có giá trị")
    try:
        value = float(pm25)
    except (TypeError, ValueError):
        raise ValueError(f"PM2.5 không phải số: {pm25!r}") from None
    if math.isnan(value) or value < 0:
        raise ValueError(f"PM2.5 không hợp lệ: {pm25!r}")
    value = round(value, 1)
    for index, (upper, label) in enumerate(BANDS):
        if value <= upper:
            return AqiLevel(index, label)
    raise AssertionError("không tới được: nhóm cuối không có cận trên")
