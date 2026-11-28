# Nhận diện nguồn bụi bằng AI (bản demo, logistic regression)

Bộ nhận diện chạy trong `aiot-controller`. Cứ 5 giây nó nhìn **120 giây mẫu 1 giây gần nhất**, so với **nền của phòng**, rồi chọn một trong 3 nhãn. Quạt và còi theo bảng cố định bên dưới. Mô hình chỉ chọn nhãn; hành động ứng với nhãn là luật viết sẵn.

| Nhãn        | Ý nghĩa                                                     | Quạt | Còi |
| ----------- | ----------------------------------------------------------- | ---- | --- |
| `normal`    | không có thay đổi bất thường so với nền phòng               | tắt  | im  |
| `cooking`   | nấu ăn / hơi nước: bụi tăng **cùng** độ ẩm hoặc nhiệt độ tăng | bật  | im  |
| `pollution` | ô nhiễm bụi mịn: bụi tăng mà độ ẩm, nhiệt độ không đổi (khói thuốc, nhang, bụi) | bật  | kêu |

Đổi từ bản trước (28/11/2026): bỏ RandomForest 4 nhãn. "Ô nhiễm từ ngoài vào" và "khói thuốc/nhang" được gộp thành `pollution`, vì:
- trên PMS7003 cả hai đều là "bụi tăng, ẩm và nhiệt không đổi", mô hình tuyến tính không tách được;
- "ô nhiễm từ ngoài vào" không tạo ra được khi demo;
- bản cũ dùng ngưỡng tuyệt đối 55,5 nên tối 23/11 (nền khoảng 160) trạm báo ô nhiễm suốt buổi.

Mục tiêu của bản này là **demo**, không phải độ chính xác ngoài thực tế. Dữ liệu huấn luyện là dữ liệu sinh ra, hiệu chỉnh theo số liệu thật của trạm. Báo cáo phải ghi rõ đây là proof of concept.

Những gì vẫn giữ nguyên:
- Ghi đè `/fan`, `/buzzer`, `/mute` vẫn được ưu tiên.
- Mất dữ liệu PM quá 2 phút thì giữ nguyên trạng thái.
- `min_state_s` vẫn chống quạt bật/tắt liên tục.
- Làm mượt: trung bình xác suất 3 lần đoán gần nhất, chỉ đổi nhãn khi ≥ 0,6.

Quay về luật ngưỡng PM2.5 khi không có file mô hình, mẫu 1 giây mới nhất cũ hơn 15 s, hoặc chưa đủ tin cậy.

**Đánh đổi cần ghi trong báo cáo:** mô hình không nhìn mức bụi tuyệt đối. Phòng ô nhiễm nhưng **ổn định** (ví dụ nền 160 cả buổi tối) được gọi là `normal`, nên quạt không tự bật. Cảnh báo AQI qua Telegram vẫn chạy riêng; gửi `/fan on` để bật tay.

## Mô hình

**Nền phòng:** phân vị 10 của 30 phút gần nhất, tính riêng cho PM2.5, độ ẩm, nhiệt độ. Sự kiện ngắn hơn khoảng 27 phút không làm nền xê dịch. Nên đặt trạm lên bàn demo trước 10–30 phút.

**Đặc trưng** (`aiot/source_window.py`, 77 số): cửa sổ 120 s chia thành 24 khối 5 giây. Mỗi khối có:

| Đặc trưng | Công thức | Ý nghĩa |
| --------- | --------- | ------- |
| `d25_k` | log(PM2.5 + 5) − log(nền + 5) | bụi lệch bao nhiêu so với nền, không phụ thuộc mức nền |
| `ch_k`  | max(d25_k − 0,1; 0) × max(ẩm − nền ẩm; 0) | bụi tăng **kèm** ẩm tăng |
| `ct_k`  | max(d25_k − 0,1; 0) × max(nhiệt − nền nhiệt; 0) | bụi tăng **kèm** nhiệt tăng |

Cộng thêm giá trị lớn nhất trong 30 s cuối của `d25`, `ch`, `ct`; `max_jump_z` là cú nhảy lớn nhất so với độ dao động thường ngày của phòng; `miss_frac` là tỉ lệ khối thiếu mẫu.

- `+5 µg/m³` giúp phòng rất sạch (nền khoảng 9) không nhảy nhãn chỉ vì PMS7003 trả số nguyên.
- Dùng **tích** thay cho chuỗi độ ẩm thô: bản đầu đưa độ ẩm thô vào thì mô hình học thành "ẩm tăng là nấu ăn" và báo nhầm khi chỉ có ẩm tăng. Ẩm hoặc nhiệt tăng một mình cho tích bằng 0.
- Số đọc PM trên 1000 (cảm biến bão hoà, nhang thật lên tới 1513) được `aiot.db.fast_rows` ghim về 1000, không coi là thiếu mẫu.

**Phân loại:** `StandardScaler` rồi `LogisticRegression` đa lớp (`class_weight="balanced"`, C = 10 chọn theo điểm demo trên tập val). Trên Pi tính softmax bằng numpy. File `models/source_logreg.json` nặng 9 KB; một lần đoán mất khoảng 0,6 ms trên laptop. Điểm bất thường `anomaly = 1 − P(normal)` được ghi kèm xác suất vào `source_predictions.proba`.

**Hệ số lớn nhất** (đặc trưng đã chuẩn hoá; khối 23 là 5 giây mới nhất), dùng để giải thích:

| Nhãn | Hệ số dương lớn | Hệ số âm lớn |
| ---- | --------------- | ------------ |
| `cooking` | `ch_23` +11,0; `ch_max` +4,8; `ct_max` +2,5 | `d25_max` −3,1 |
| `pollution` | `d25_23` +5,6; `d25_max` +3,3 | `ch_max` −6,1; `ch_23` −5,4; `ct_max` −3,5 |
| `normal` | | `ch_23` −5,6; `d25_23` −4,4; `ct_23` −2,1 |

Nói cách khác: bụi lệch khỏi nền là bất thường; nếu bất thường đi kèm ẩm/nhiệt tăng thì là nấu ăn, không kèm thì là bụi mịn.

## Dữ liệu

**Số liệu thật** (kéo từ Pi ngày 28/11/2026 bằng `training.export_fast`, lưu ở `training/data/`):

| File | Nội dung |
| ---- | -------- |
| `fast_readings.csv` | 20 856 mẫu 1 giây, 23/11 21:50 → 28/11 22:43 (phần lớn là ngày 28/11) |
| `raw_readings.csv`  | 2 935 mẫu 10 giây, 23/11 19:47 → 28/11 |
| `readings.csv`      | 218 cửa sổ 5 phút, 07/11 → 28/11 |

Các sự kiện thật:
- **Nhang 21:07:17 ngày 23/11** (người dùng xác nhận): nền khoảng 170, hai đỉnh 805 và 1061 µg/m³.
- **Gai bụi 08:20:35 ngày 28/11:** PM2.5 tăng 47 → 1513 trong khoảng 25 s, về nền sau khoảng 70 s, PM10/PM2.5 ≈ 2, ẩm và nhiệt không đổi. Chưa rõ nguồn.
- **Nền nhảy bậc 16:03–16:06 ngày 28/11:** khoảng 30 lên khoảng 97 µg/m³, cùng lúc ẩm 61 → 54 %, nhiệt 25,3 → 24,1 °C. Có thể do mở cửa hoặc chuyển chỗ trạm.

Nền giữa các ngày rất khác nhau (trung vị 27 tới 166 µg/m³) và **chưa có lần nấu ăn thật nào**.

**Dữ liệu sinh ra** (`training/demo_data.py`, seed 42): 780 phiên, 202 071 cửa sổ.
- `fit_real()` rút từ số liệu thật:
  - nhiễu nền: độ lệch chuẩn log PM2.5 khoảng 0,016, thời gian tương quan khoảng 15 s (sáng 28/11);
  - PM1/PM2.5 ≈ 0,68, PM10/PM2.5 ≈ 1,21;
  - dải ẩm 53–72 %, dải nhiệt 24,7–28 °C;
  - hai đường cong bụi thật ở trên.
- Kết quả được lưu ở `training/data/demo_real.json` để Pi không cần pandas hay CSV.
- Mức nền rải log-đều từ 5 đến 250 µg/m³.
- Các kiểu phiên:
  - `idle`: để yên;
  - `dust`: đường cong bụi thật, co giãn biên độ và thời gian, 1–3 đợt;
  - `cooking`: **mô phỏng** hơi nước. Ẩm +5–20 %, nhiệt +0,5–3 °C; bụi chỉ xuất hiện sau khi ẩm đã tăng và tắt ngay khi hết hơi;
  - `hum_only`, `temp_only`, `small_bump`: ẩm, nhiệt hoặc bụi tăng nhẹ một mình, đều là `normal`;
  - `step_up`, `step_down`: nền nhảy bậc như 16:05 ngày 28/11;
  - `back_to_back`: bụi mịn rồi nấu ăn ngay sau đó.
- **Nhãn từng giây** tính trên tín hiệu sạch, cùng định nghĩa nền với đặc trưng:
  - `pollution` khi (PM2.5 + 5) / (nền + 5) ≥ 1,3 và không nóng ẩm;
  - `cooking` khi tỉ số đó ≥ 1,2 và ẩm ≥ nền + 3 % hoặc nhiệt ≥ nền + 1,5 °C;
  - còn lại là `normal`.

## Kết quả (huấn luyện ngày 28/11/2026)

Chia theo phiên 546/117/117 (train/val/test), chia trong từng kiểu phiên.

| Lớp (test sinh ra, từng cửa sổ) | precision | recall | F1 |
| ------------------------------- | --------- | ------ | -- |
| normal    | 0,998 | 0,991 | 0,995 |
| cooking   | 0,975 | 0,993 | 0,984 |
| pollution | 0,959 | 0,993 | 0,976 |
| macro     | 0,977 | 0,992 | 0,985 |

**Tiêu chí demo** (phát lại theo thời gian qua bước làm mượt giống controller):

| Tiêu chí | Kết quả | Mục tiêu |
| -------- | ------- | -------- |
| Để yên (kể cả ẩm/nhiệt/bụi tăng nhẹ, nền giảm) ra `normal` | 99,97 % | ≥ 99 % |
| Trung vị P(normal) khi để yên | 0,997 | ≥ 0,9 |
| Báo nhầm khi để yên | 0,04 lần/giờ | ≤ 0,2 |
| Nấu ăn ra `cooking` trong ≤ 30 s | 96 % (25/26), trễ trung vị 5 s | ≥ 95 % |
| Bụi mịn ra `pollution` trong ≤ 15 s | 100 % (72/72), trễ trung vị 5 s | ≥ 95 % |
| Trung vị xác suất nhãn đúng trong sự kiện | 1,0 | ≥ 0,8 |
| Về `normal` ≤ 60 s sau khi tín hiệu hết | 100 %, trung vị 15 s | ≥ 95 % |
| Số liệu thật ngoài sự kiện ra `normal` | 98,3 % | ≥ 98 % |

**Trên số liệu thật:**
- Nhang 23/11 ra `pollution` sau 10 s, gai bụi 08:20 sau 5 s, nền nhảy bậc 16:07 sau 5 s, cả ba với xác suất 1,0.
- Nền khoảng 160 tối 23/11 ra `normal` 99 % số cửa sổ.
- Phần chưa khớp nằm ở 11:12–11:21 ngày 28/11: nền dâng nhẹ 45 → 51, sát ngưỡng 1,3. Luật tham chiếu và mô hình nhảy qua lại quanh ngưỡng.

**Demo ngoại tuyến** (phát lại `inject_pm.py` qua `controller.step` trên DB thử, `config.yaml` thật):

| Kịch bản | Nhãn AI | Quạt | Còi |
| -------- | ------- | ---- | --- |
| normal (nền 9 µg/m³) | `normal` suốt 15 phút | tắt | im |
| cooking (nền khoảng 105, ẩm +8 %) | `cooking` sau khoảng 10 s, về `normal` sau khi hết hơi | bật | im |
| pollution (nền 17 và 124) | `pollution` sau khoảng 10 s, về `normal` sau 15–45 s | bật | kêu |
| back_to_back (nền 223, đỉnh bão hoà 1000) | `pollution` mỗi đợt, về `normal` giữa các đợt | bật | kêu |

**Giới hạn cần ghi trong báo cáo:**
- Toàn bộ dữ liệu huấn luyện là dữ liệu sinh ra. Phần thật chỉ có 2 sự kiện bụi và khoảng 9 giờ nền.
- `cooking` chưa từng được đo thật; cách mô phỏng hơi nước là giả định.
- Nền phòng ô nhiễm mà ổn định được coi là `normal`.
- Sự kiện kéo dài quá khoảng 27 phút sẽ dần trở thành nền mới.

## Thành phần

| File | Việc |
| ---- | ---- |
| `aiot/source_window.py` | bộ đệm 30 phút, 77 đặc trưng cửa sổ; dùng chung cho Pi và laptop |
| `aiot/source_model.py` | 3 nhãn, `LogRegModel` (numpy), làm mượt, bảng hành động |
| `aiot/controller.py` (`_classify`), `aiot/db.py` (`fast_rows`) | chạy mô hình mỗi 5 s, ghi `source_predictions` (kèm `anomaly`) |
| `training/demo_data.py` | rút tham số từ số liệu thật, sinh phiên có nhãn từng giây |
| `training/train_logreg.py` | huấn luyện, chọn C, chấm điểm demo, chấm trên số liệu thật, xuất mô hình |
| `training/export_fast.py`, `scripts/label_event.py` | kéo dữ liệu từ Pi; gán nhãn sự kiện thật |
| `scripts/inject_pm.py --scenario normal\|cooking\|pollution\|back_to_back` | phát lại kịch bản trên DB thử để demo |

## Huấn luyện lại

```powershell
.venv\Scripts\python -m training.export_fast --host admin@pi.local   # kéo dữ liệu Pi về training/data/
.venv\Scripts\python -m training.train_logreg --no-cache             # khoảng 2–3 phút; mã thoát 1 nếu có tiêu chí chưa đạt
.\scripts\up.ps1                                                     # chép code (bản đã commit) + models/source_logreg.json lên Pi
```

Kết quả chi tiết nằm ở `models/source_logreg_summary.json`.

## Việc tiếp theo: tập dượt với đạo cụ

1. Đặt trạm lên bàn demo trước 10–30 phút để nền ổn định.
2. Thử mỗi đạo cụ 3 lần, sau mỗi lần thì gán nhãn: `venv/bin/python scripts/label_event.py --label cooking --start now --duration 300 --note "cốc nước sôi"`.
   - Nấu ăn: cốc nước sôi hoặc mì cốc, cách cảm biến 20–30 cm.
   - Bụi mịn: que nhang hoặc que diêm vừa thổi tắt, cách cảm biến 1 m. Làm nơi thoáng, có người trông.
3. Xem nhãn và xác suất trên web, hoặc `SELECT ts, label, proba FROM source_predictions ORDER BY ts DESC LIMIT 20`.
4. Nếu nước sôi không ra `cooking`: kéo dữ liệu về, so độ ẩm, nhiệt và bụi thật với khoảng tham số trong `_cooking_event` của `training/demo_data.py`, chỉnh cho khớp rồi huấn luyện lại.
