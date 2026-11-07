"""
Chỉ số đánh giá dự báo PM2.5 theo từng horizon (Task 12).

Chỉ dùng numpy và aiot.aqi.classify (không cần pandas, scikit-learn hay TensorFlow), nên
chạy được cả trên Pi khi đánh giá với dữ liệu của trạm.

- y_true, y_pred: mảng (số cửa sổ, số horizon), µg/m³; cột h là horizon h + 1.
- Dự báo âm được cắt về 0 trước khi tính, giống run_forecast trong aiot/forecast.py.
- Nhóm AQI theo aiot.aqi.classify (US EPA 2024, làm tròn 1 chữ số), dùng chung với bot.
- Sự kiện vượt ngưỡng: giá trị thật thuộc nhóm "Xấu" trở lên (PM2.5 >= 55,5 µg/m³).
  exceedance_recall = số sự kiện mà dự báo cũng >= "Xấu" / số sự kiện; None khi không có
  sự kiện nào ở horizon đó (không chia cho 0).
"""
import numpy as np

from aiot.aqi import LABELS, classify

EXCEEDANCE_INDEX = LABELS.index("Xấu")          # 3: PM2.5 >= 55,5 µg/m³


def aqi_index(values) -> np.ndarray:
    """Chỉ số nhóm AQI (0 = Tốt ... 5 = Nguy hại) của từng phần tử, giữ nguyên hình dạng."""
    arr = np.asarray(values, dtype=float)
    return np.array([classify(v).index for v in arr.ravel()], dtype=int).reshape(arr.shape)


def evaluate(y_true, y_pred) -> dict:
    """MAE, RMSE, độ chính xác nhóm AQI và độ nhạy vượt ngưỡng theo từng horizon.

    Trả về {"mae": [..], "rmse": [..], "aqi_accuracy": [..], "exceedance_recall": [..],
    "n": số cửa sổ}; mỗi danh sách có một phần tử cho mỗi horizon."""
    t = np.asarray(y_true, dtype=float)
    p = np.asarray(y_pred, dtype=float)
    if t.ndim != 2 or t.shape != p.shape:
        raise ValueError(f"y_true {t.shape} và y_pred {p.shape} phải cùng dạng (cửa sổ, horizon)")
    if t.shape[0] == 0:
        raise ValueError("không có cửa sổ nào để đánh giá")
    if not (np.isfinite(t).all() and np.isfinite(p).all()):
        raise ValueError("y_true/y_pred có NaN hoặc vô cực")
    p = np.maximum(p, 0.0)

    err = p - t
    mae = np.abs(err).mean(axis=0)
    rmse = np.sqrt((err ** 2).mean(axis=0))
    gt, gp = aqi_index(t), aqi_index(p)
    acc = (gt == gp).mean(axis=0)

    recall = []
    for h in range(t.shape[1]):
        events = gt[:, h] >= EXCEEDANCE_INDEX
        n_events = int(events.sum())
        recall.append(None if n_events == 0
                      else float((gp[events, h] >= EXCEEDANCE_INDEX).sum() / n_events))
    return {
        "mae": [float(v) for v in mae],
        "rmse": [float(v) for v in rmse],
        "aqi_accuracy": [float(v) for v in acc],
        "exceedance_recall": recall,
        "n": int(t.shape[0]),
    }
