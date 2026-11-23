# Nhận diện nguồn bụi bằng AI

Bộ nhận diện chạy trong `aiot-controller`. Cứ 5 giây nó đoán nguồn bụi từ mẫu 1 giây của 30 phút qua, rồi bật/tắt quạt và còi theo bảng cố định dưới đây. Mô hình chỉ chọn nhãn. Hành động ứng với mỗi nhãn là luật viết sẵn, không do mô hình quyết định.

| Nhãn        | Ý nghĩa                                       | Quạt | Còi |
| ----------- | --------------------------------------------- | ---- | --- |
| `normal`    | không có nguồn bụi, nền PM2.5 < 55,5          | tắt  | im  |
| `cooking`   | nấu ăn                                        | bật  | im  |
| `pollution` | ô nhiễm từ ngoài vào, nền PM2.5 ≥ 55,5        | bật  | kêu |
| `smoking`   | khói thuốc lá / nhang                         | tắt  | kêu |

Những gì vẫn giữ nguyên:
- Ghi đè `/fan` và `/mute` vẫn được ưu tiên.
- Mất dữ liệu PM quá 2 phút thì giữ nguyên trạng thái.
- `min_state_s` vẫn chống quạt bật/tắt liên tục.

Quay về luật ngưỡng PM2.5 cũ khi:
- không có file mô hình,
- mẫu 1 giây mới nhất cũ hơn 15 s,
- hoặc trung bình xác suất của 3 lần đoán gần nhất dưới 0,6.

## Thành phần

| File | Việc |
| ---- | ---- |
| `aiot/pms7003.py` | thêm giải mã 6 kênh đếm hạt (byte 16–27) |
| `aiot/sensors.py` (`SensorHub`), `aiot/collector.py` | mỗi cảm biến chạy một luồng; ghi `fast_readings` 1 giây, vẫn ghi `raw_readings`/CSV/MQTT 10 giây |
| `aiot/source_features.py` | 22 đặc trưng: mức, động học, tỉ lệ hạt, độ ẩm/nhiệt/ánh sáng; dùng chung cho Pi và laptop |
| `aiot/source_model.py` | đọc rừng cây dạng JSON bằng numpy (không cần scikit-learn trên Pi), làm mượt kết quả, bảng hành động |
| `aiot/control.py`, `aiot/alarm.py`, `aiot/controller.py` | quạt và còi theo nhãn; sự kiện `source_change` và tin Telegram |
| `training/simulate_sources.py` | sinh dữ liệu mô phỏng 4 nhãn |
| `training/train_source.py` | huấn luyện, đánh giá, xuất `models/source_rf.json` |
| `training/export_fast.py`, `scripts/label_event.py` | kéo dữ liệu từ Pi về; gán nhãn sự kiện thật |
| `scripts/inject_pm.py --scenario …` | phát lại kịch bản trên DB thử để demo |

## Dữ liệu

**Dữ liệu thật** (kéo về ngày 23/11/2026, `training/data/raw_readings.csv`, mỗi mẫu cách 10 s):

| Đoạn | Mô tả | Nhãn |
| ---- | ----- | ---- |
| 19:48–21:07 và 21:09–21:31 | nền PM2.5 156–173 µg/m³; PM1/PM2.5 ≈ 0,61, PM10/PM2.5 ≈ 1,18; ẩm 71–77 %; đèn 220 hoặc 1030 lux | `pollution` (giả định: không có nguồn bụi trong phòng) |
| 21:07:12–21:09:00 | **đốt nhang** (người dùng xác nhận): hai đỉnh 805 và 1061 µg/m³ cách nhau khoảng 50 s; giảm 75 % trong khoảng 20 s; phần vượt nền có PM1/PM2.5 0,27–0,42, PM10/PM2.5 1,4–2,0; nhiệt và ẩm không đổi | `smoking` |

Các nhãn này đã ghi vào bảng `source_labels` trên Pi và vào file `training/data/real_labels.csv`.

**Dữ liệu mô phỏng** (400 phiên, mỗi phiên 40–100 phút, seed 42):
- Tham số nền lấy từ số liệu thật ở trên và từ `readings` ngày 07–09/11 (nền sạch, PM1/PM2.5 ≈ 0,69).
- Mẫu `smoking` được chỉnh theo đợt nhang: dạng điếu thuốc, nhang ngắn và que nhang cháy lâu.
- **Mẫu `cooking` là giả định** vì chưa có lần nấu ăn thật nào được đo. Mô phỏng cho bụi tăng dần trong vài phút, ẩm +3–15 %, nhiệt +0,3–2 °C, PM1/PM2.5 0,5–0,75, PM10/PM2.5 1,15–1,5.
- Phần nhiễu thêm vào: tắm (ẩm tăng mà không có bụi), bật/tắt đèn, khung lỗi đơn lẻ, mất mẫu. DHT11 làm tròn và chỉ cập nhật 2 s một lần.
- Kênh đếm hạt có thu nhưng **chưa dùng làm đặc trưng**, vì chưa có số liệu thật để mô phỏng cho đúng.

## Kết quả (huấn luyện ngày 23/11/2026)

Tập dữ liệu được chia theo phiên 280/60/60, tổng 288 220 cửa sổ 5 giây. Mô hình là RandomForest gồm 100 cây, `max_depth=12` (chọn theo tập val).

| Mô hình (test mô phỏng) | macro F1 |
| ----------------------- | -------- |
| Luật ngưỡng viết tay | 0,749 |
| Cây quyết định độ sâu 4 | 0,875 |
| **RandomForest** | **0,927** |

| Lớp | precision | recall | F1 |
| --- | --------- | ------ | -- |
| normal | 0,972 | 0,947 | 0,959 |
| cooking | 0,921 | 0,855 | 0,887 |
| pollution | 0,916 | 0,965 | 0,940 |
| smoking | 0,890 | 0,954 | 0,921 |

Theo thời gian, sau khi làm mượt (3 lần đoán, ngưỡng 0,6):
- Phát hiện đủ 24/24 sự kiện hút thuốc, độ trễ trung vị 15 s (p90 26 s).
- Phát hiện đủ 23/23 sự kiện nấu ăn, độ trễ trung vị 111 s (p90 173 s).
- Báo nhầm 0,17 lần/giờ trong thời gian bình thường.
- Nhầm nhiều nhất là nấu ăn ↔ ô nhiễm khi nền đã cao.

**Trên dữ liệu thật** (mẫu 10 s được giữ nguyên thành chuỗi 1 s):

| Đoạn | RandomForest đã làm mượt | Luật viết tay |
| ---- | ------------------------ | ------------- |
| Nền ô nhiễm (829 cửa sổ) | 99,4 % đúng (5 cửa sổ ngay sau nhang bị đoán `smoking`) | 100 % |
| Đốt nhang (22 cửa sổ) | 86 % đúng (3 cửa sổ đầu là `pollution`, tức trễ khoảng 15 s) | 68 % |

Trên Pi 4, một lần tính đặc trưng và dự đoán mất khoảng 10 ms; nạp mô hình (6,2 MB) mất 0,6 s.

Demo ngoại tuyến (`inject_pm.py --scenario …` rồi chạy `controller.step` mỗi 5 s, mỗi kịch bản 10 phút) cho kết quả đúng bảng hành động ở cả 4 kịch bản:

| Kịch bản | Nhãn | Quạt | Còi |
| -------- | ---- | ---- | --- |
| normal | `normal` | tắt | không kêu |
| cooking | `cooking` sau khoảng 2,5 phút | bật | không kêu |
| pollution | `pollution` | bật | kêu, lặp mỗi 5 phút |
| smoking | `smoking` ngay sau làn khói đầu (≤ 10 s) | tắt | kêu |

**Giới hạn — cần ghi rõ trong báo cáo:**
- Các con số trên chủ yếu đo trên dữ liệu mô phỏng. Phần thật mới có 1 lần đốt nhang và khoảng 1,7 giờ nền cao.
- `cooking` chưa được kiểm tra trên dữ liệu thật.
- Nhãn `pollution` của tối 23/11 là giả định của nhóm.
- Nhang chỉ là chất thay thế cho khói thuốc lá. Trên PMS7003 khói nhang ra hạt khá thô, PM10/PM2.5 khoảng 1,9.

## Việc tiếp theo: thu sự kiện thật

1. Sau khi khởi động lại dịch vụ, kiểm tra `fast_readings` có khoảng 1 dòng mỗi giây:
   `venv/bin/python -c "import sqlite3;c=sqlite3.connect('/home/admin/iot_data/aiot.db');print(c.execute('select count(*),max(ts) from fast_readings').fetchone())"`
2. Mỗi loại làm 3–5 lần. Ngay sau mỗi lần thì gán nhãn:
   `venv/bin/python scripts/label_event.py --label cooking --start 18:05 --end 18:25 --note "rán trứng"`
   - Nấu ăn: rán, xào hoặc đun nước, cách cảm biến 1–3 m, 10–20 phút.
   - Khói: nhang hoặc thuốc lá (do người vốn hút thuốc), cách cảm biến 1–2 m, 5–8 phút. Làm nơi thoáng và luôn có người trông.
   - Ô nhiễm: buổi tối nền cao, mở cửa sổ, ghi ít nhất 1 giờ.
   - Bình thường: vài giờ không có hoạt động gì, có thêm tắm và bật/tắt đèn.
3. Kéo dữ liệu về: `.venv\Scripts\python -m training.export_fast --host admin@<IP Pi>`.
   Thêm các dòng nhãn mới vào `training/data/real_labels.csv`, hoặc xuất từ bảng `source_labels.csv`.
4. Chỉnh tham số `cooking_event` và `smoking_event` trong `training/simulate_sources.py` cho khớp đường đo thật. Sau đó chạy:
   `.venv\Scripts\python -m training.train_source --no-cache`
   Kết quả trên `fast_1s` (dữ liệu thật 1 giây) sẽ có trong `models/source_summary.json`.
5. Chép mô hình mới lên Pi (`scripts/up.ps1` tự chép `models/source_rf.json`); controller tự khởi động lại.
