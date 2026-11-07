# Nguồn dữ liệu `hanoi_openmeteo.csv`

Dữ liệu công khai dùng để **huấn luyện sơ bộ (cold start)** khi dữ liệu của trạm trên Pi còn quá ngắn. Không phải dữ liệu đo của trạm.

- Tạo bằng: `python training/fetch_public.py` (mặc định `--start 2022-08-01 --end <hôm qua>`)
- Ngày tải: 07/11/2026
- Vị trí: Hà Nội, lat 21.0285, lon 105.8542 (API trả về ô lưới gần nhất: 21.0, 105.9 cho chất lượng không khí; 21.05, 105.90 cho thời tiết)
- Múi giờ: `Asia/Ho_Chi_Minh` (UTC+7); cột `timestamp` là đầu giờ địa phương kèm độ lệch `+07:00`
- Khoảng thời gian: 2022-08-04 07:00 đến 2026-11-06 23:00, 37 337 giờ liên tục, không có giá trị trống. Trước khoảng 04/08/2022, API trả `pm2_5` toàn null (dữ liệu CAMS toàn cầu của Open-Meteo bắt đầu từ đó), nên script bỏ phần đầu này.

## Cột

| Cột | Biến Open-Meteo | Đơn vị | API |
| --- | --- | --- | --- |
| `pm25` | `pm2_5` | µg/m³ | Air Quality |
| `pm10` | `pm10` | µg/m³ | Air Quality |
| `temp` | `temperature_2m` | °C | Historical Weather |
| `hum` | `relative_humidity_2m` | % | Historical Weather |
| `wind` | `wind_speed_10m` | km/h | Historical Weather |
| `precip` | `precipitation` | mm | Historical Weather |

## API

- Air Quality: `https://air-quality-api.open-meteo.com/v1/air-quality?latitude=21.0285&longitude=105.8542&hourly=pm2_5,pm10&timezone=Asia/Ho_Chi_Minh&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`
- Historical Weather: `https://archive-api.open-meteo.com/v1/archive?latitude=21.0285&longitude=105.8542&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation&timezone=Asia/Ho_Chi_Minh&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`
- Gọi theo từng năm, không cần API key. Tên biến đã kiểm tra bằng cách gọi API thật ngày 07/11/2026 (API báo lỗi nếu tên biến sai).

## Lưu ý quan trọng

- **PM2.5 và PM10 là đầu ra mô hình CAMS** (Copernicus Atmosphere Monitoring Service) do Open-Meteo phân phối, **không phải số đo của trạm quan trắc**. Lưới mô hình thô (mỗi ô cỡ hàng chục km), nên mức tuyệt đối và dao động trong ngày có thể khác cảm biến PMS7003 đặt trong nhà.
- Thời tiết là dữ liệu tái phân tích/mô hình (ERA5 và các mô hình khác qua Historical Weather API).
- Chỉ dùng để huấn luyện sơ bộ và phân tích phương pháp; mô hình cuối phải được đánh giá lại trên dữ liệu của trạm.

## Giấy phép

Dữ liệu Open-Meteo theo giấy phép **CC BY 4.0**. Khi dùng trong báo cáo phải ghi nguồn: "Weather and air quality data by Open-Meteo.com" (https://open-meteo.com/), kèm nguồn gốc CAMS cho dữ liệu chất lượng không khí (Copernicus Atmosphere Monitoring Service).

## Nguồn tuỳ chọn chưa dùng

- **OpenAQ** (số đo trạm thật, ví dụ trạm Đại sứ quán Mỹ tại Hà Nội): cần API key (v3), nên chưa tải. Có thể bổ sung sau để đối chiếu với dữ liệu CAMS và với cảm biến của trạm.
