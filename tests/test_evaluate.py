"""Test chỉ số đánh giá dự báo theo từng horizon (Task 12, chỉ cần numpy)."""
import math

import numpy as np
import pytest

import aiot.aqi
from training import evaluate as ev
from training.evaluate import evaluate


def test_metrics_on_known_values():
    # 3 cửa sổ x 2 horizon, tính tay
    y_true = np.array([[10.0, 20.0],
                       [30.0, 40.0],
                       [50.0, 60.0]])
    y_pred = np.array([[12.0, 20.0],
                       [27.0, 44.0],
                       [50.0, 50.0]])
    out = evaluate(y_true, y_pred)
    # horizon 1: sai số 2, 3, 0 -> MAE 5/3, RMSE sqrt(13/3)
    # horizon 2: sai số 0, 4, 10 -> MAE 14/3, RMSE sqrt(116/3)
    assert out["mae"] == pytest.approx([5 / 3, 14 / 3])
    assert out["rmse"] == pytest.approx([math.sqrt(13 / 3), math.sqrt(116 / 3)])
    # nhóm AQI: 10 TB/12 TB, 30 TB/27 TB, 50 Kém/50 Kém -> 3/3
    #           20 TB/20 TB, 40 Kém/44 Kém, 60 Xấu/50 Kém -> 2/3
    assert out["aqi_accuracy"] == pytest.approx([1.0, 2 / 3])
    # vượt ngưỡng (>= Xấu): horizon 1 không có sự kiện; horizon 2 có 1 sự kiện, bị bỏ lỡ
    assert out["exceedance_recall"] == [None, 0.0]
    assert out["n"] == 3


def test_exceedance_recall_when_no_events():
    y_true = np.full((4, 6), 20.0)
    y_pred = np.full((4, 6), 80.0)         # dự báo vượt nhưng thực tế không có sự kiện
    out = evaluate(y_true, y_pred)
    assert out["exceedance_recall"] == [None] * 6
    assert len(out["mae"]) == 6


def test_exceedance_recall_counts_hits():
    y_true = np.array([[60.0], [130.0], [70.0], [10.0]])
    y_pred = np.array([[56.0], [40.0], [200.0], [90.0]])
    # 3 sự kiện (60, 130, 70), bắt được 2 (56 -> Xấu, 200 -> Rất xấu)
    assert evaluate(y_true, y_pred)["exceedance_recall"] == [pytest.approx(2 / 3)]


def test_aqi_accuracy_uses_shared_classifier():
    assert ev.classify is aiot.aqi.classify
    # aiot.aqi.classify làm tròn 1 chữ số: 9,04 là Tốt, 9,06 là Trung bình
    y_true = np.array([[9.04], [9.06], [55.45]])
    y_pred = np.array([[9.0], [9.04], [55.5]])
    out = evaluate(y_true, y_pred)
    assert out["aqi_accuracy"] == [pytest.approx(2 / 3)]
    # 55,45 làm tròn thành 55,5 nên đã là Xấu: một sự kiện, bắt được
    assert out["exceedance_recall"] == [1.0]


def test_negative_predictions_clipped_like_forecast():
    # run_forecast cắt dự báo âm về 0, đánh giá cũng vậy
    out = evaluate(np.array([[0.0]]), np.array([[-5.0]]))
    assert out["mae"] == [0.0] and out["aqi_accuracy"] == [1.0]


def test_rejects_bad_shapes_and_nan():
    with pytest.raises(ValueError):
        evaluate(np.zeros((3, 6)), np.zeros((3, 5)))
    with pytest.raises(ValueError):
        evaluate(np.zeros((0, 6)), np.zeros((0, 6)))
    with pytest.raises(ValueError):
        evaluate(np.array([[math.nan]]), np.array([[1.0]]))
