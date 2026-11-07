"""Test phân nhóm AQI theo PM2.5 (US EPA 2024)."""
import math

import pytest

from aiot.aqi import AqiLevel, classify


@pytest.mark.parametrize("pm25,label", [
    (0.0, "Tốt"),
    (9.0, "Tốt"),
    (9.1, "Trung bình"),
    (35.4, "Trung bình"),
    (35.5, "Kém"),
    (55.4, "Kém"),
    (55.5, "Xấu"),
    (125.4, "Xấu"),
    (125.5, "Rất xấu"),
    (225.4, "Rất xấu"),
    (225.5, "Nguy hại"),
    (999.0, "Nguy hại"),
])
def test_aqi_boundaries(pm25, label):
    assert classify(pm25).label == label


def test_aqi_index_order():
    labels = ["Tốt", "Trung bình", "Kém", "Xấu", "Rất xấu", "Nguy hại"]
    values = [5, 20, 45, 100, 200, 300]
    assert [classify(v) for v in values] == [AqiLevel(i, l) for i, l in enumerate(labels)]


def test_aqi_rounds_to_one_decimal():
    assert classify(9.04).label == "Tốt"
    assert classify(9.06).label == "Trung bình"
    # không có khoảng trống giữa hai nhóm: mọi giá trị đều có nhóm
    assert classify(35.46).label == "Kém"
    assert classify(55.449).label == "Kém"
    assert classify(225.46).label == "Nguy hại"


@pytest.mark.parametrize("bad", [None, -0.1, float("nan"), math.inf * -1])
def test_aqi_rejects_none_and_negative(bad):
    with pytest.raises(ValueError):
        classify(bad)
