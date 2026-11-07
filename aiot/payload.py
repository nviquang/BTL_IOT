"""
Định dạng dòng cảm biến dùng chung giữa Pi (collector) và laptop (server_app.py).

- CSV giữ đúng định dạng cũ: timestamp,device_id,temperature_c,humidity_pct,lux.
- Payload MQTT là dòng CSV cộng thêm pm1, pm25, pm10, mq135_do (có thể là None).
- Module này không import thư viện nặng, để server_app và test đều dùng được.
"""
from datetime import datetime

CSV_HEADER = ["timestamp", "device_id", "temperature_c", "humidity_pct", "lux"]


def valid_sensor_row(row):
    """Kiểm tra mẫu cảm biến có đủ khoá và 3 giá trị đo là số hợp lệ.

    Trả False nếu thiếu khoá -> bỏ qua mẫu, tránh ghi dòng rỗng (NaN) làm
    hỏng MinMaxScaler/window_transform trong notebook.
    """
    if not isinstance(row, dict):
        return False
    for key in ("timestamp", "device_id"):
        if key not in row:
            return False
    for key in ("temperature_c", "humidity_pct", "lux"):
        try:
            float(row[key])
        except (KeyError, TypeError, ValueError):
            return False
    return True


def build_csv_row(now: datetime, device_id: str, temp: float, hum: float, lux: float) -> dict:
    """Dòng CSV như client cũ: giờ địa phương đến giây, giá trị làm tròn 1 chữ số."""
    return {
        "timestamp": now.isoformat(timespec="seconds"),
        "device_id": device_id,
        "temperature_c": round(float(temp), 1),
        "humidity_pct": round(float(hum), 1),
        "lux": round(float(lux), 1),
    }


def build_payload(csv_row: dict, pm1, pm25, pm10, mq135_do) -> dict:
    """Payload MQTT: dòng CSV cộng thêm bụi và bit MQ135 (None khi đọc lỗi)."""
    payload = dict(csv_row)
    payload.update(pm1=pm1, pm25=pm25, pm10=pm10, mq135_do=mq135_do)
    return payload
