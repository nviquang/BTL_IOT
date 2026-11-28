# Đề xuất AI bản 3: mô hình cho buổi demo

Ngày 26/11/2026. Bản 1 và bản 2 nhắm tới một mô hình tổng quát. Bản này đổi mục tiêu: **lên sân khấu
thì trạm phải nhận đúng, nhanh và trông đẹp**. Chỉ cần đúng trong căn phòng demo, với đúng cảm biến
đó và 4 đạo cụ cố định. Overfit theo môi trường là chủ ý. Báo cáo phải ghi rõ điều này (mục 7).

## 1. Vì sao mô hình hiện tại demo tệ

Xem lại `models/source_summary.json`, `training/data/` và dữ liệu web cloud (Pi offline từ
23/11 23:56):

| # | Vấn đề | Bằng chứng | Hậu quả khi demo |
| - | ------ | ---------- | ---------------- |
| 1 | Học gần như hoàn toàn trên dữ liệu mô phỏng | Dữ liệu thật chỉ có 435 mẫu 10 s, 1 lần đốt nhang, 0 lần nấu ăn | F1 0,93 trên mô phỏng không cho biết gì về mạch thật |
| 2 | Nhãn `pollution` dựa vào ngưỡng tuyệt đối 55,5 trong khi nền phòng là **150–190 µg/m³** cả buổi tối | Snapshot cuối: `pollution` 0,67 / `cooking` 0,32 lúc không có gì xảy ra | Trạng thái "ô nhiễm" bật thường trực, quạt và còi luôn chạy, nên không bao giờ thấy được cảnh "bình thường → có sự kiện → bình thường" |
| 3 | `cooking` chưa từng được đo, trễ trung vị 111 s (trên mô phỏng) | `rf_temporal` | Đứng chờ gần 2 phút trên sân khấu, mà nhãn có thể còn sai |
| 4 | Mỗi 5 s phân loại lại cả cửa sổ 30 phút; nền là phân vị 10 của 30 phút; có đặc trưng `since_exc` | `aiot/source_features.py` | Sau một sự kiện, mô hình cần tới 30 phút mới "quên"; hai màn demo liền nhau ảnh hưởng nhau |
| 5 | Biên giới giữa các lớp sát nhau (nấu ăn ↔ ô nhiễm là chỗ nhầm nhiều nhất) | Ma trận nhầm lẫn: 774 cửa sổ cooking bị đoán thành pollution | Nhãn nhấp nháy, xác suất lơ lửng quanh 0,6 |
| 6 | Mẫu 1 giây có thể chưa chạy: code đã chép lên Pi 23/11 nhưng chưa khởi động lại dịch vụ | CLAUDE.md | Cần kiểm tra trước khi thu dữ liệu |

Kết luận: lỗi không nằm ở RandomForest. Lỗi nằm ở chỗ **bài toán được định nghĩa cho nhà thật ở
Hà Nội, nhưng dữ liệu và điều kiện lại là phòng demo**. Ta định nghĩa lại bài toán cho khớp phòng demo.

## 2. Ý tưởng chính: "Dạy trạm nhận 4 đạo cụ"

Giữ nguyên 4 nhãn và bảng hành động. Đổi hai điều:

1. Mỗi nhãn ứng với **một đạo cụ mang lên lớp được**, chọn sao cho chúng khác nhau rõ về vật lý trên
   các cảm biến đang có.
2. "Bình thường" được tính **so với nền của phòng lúc demo**, không so với ngưỡng AQI.

| Nhãn | Đạo cụ demo | Dấu hiệu kỳ vọng (giả thuyết, đo lại ở mục 5) | Quạt | Còi |
| ---- | ----------- | --------------------------------------------- | ---- | --- |
| `normal` | Không làm gì; đi lại, bật/tắt đèn, thở gần | PM nằm trong dải nền ± nhiễu | tắt | im |
| `cooking` | Cốc nước sôi / mì cốc đặt cách cảm biến 20–30 cm | **Độ ẩm tăng mạnh** (+5–20 %), nhiệt độ tăng nhẹ, PM tăng chậm (PMS7003 đếm cả giọt hơi nước) | bật | im |
| `smoking` | Que nhang hoặc que diêm vừa thổi tắt, cách 1 m | PM1 và PM2.5 **vọt nhanh thành gai nhọn**, phần vượt nền có PM1/PM2.5 thấp (đã đo: 0,27–0,42), ẩm không đổi | tắt | kêu |
| `pollution` | Một chút **bụi phấn** hoặc bột mì phủi trước cảm biến 50 cm, hoặc vỗ khăn bụi | Hạt **thô**: PM10/PM2.5 cao, kênh đếm `n50`, `n100` tăng mạnh, ẩm không đổi | bật | kêu |

Nếu buổi thu thử cho thấy hai đạo cụ cho tín hiệu giống nhau thì **đổi đạo cụ, không đổi mô hình**.
Ví dụ, nếu phấn trông giống nhang thì thử bột mì, cát mịn hoặc bụi hút ra từ túi máy hút bụi.

## 3. Pipeline mới: theo sự kiện, không theo cửa sổ trượt

```
mẫu 1 s ─► nền phòng ─► bộ phát hiện sự kiện ─► 8 đặc trưng của sự kiện ─► kNN ─► khoá nhãn ─► bảng hành động
            (trung vị        (IDLE → EVENT → COOLDOWN)       (từ lúc bắt đầu)      (mẫu thật)   tới khi về nền
             10 phút khi IDLE,
             đóng băng khi EVENT)
```

**Nền phòng.** Trung bình trượt mũ (hằng số thời gian 5 phút) của từng kênh PM, đếm hạt, độ ẩm,
nhiệt độ. Chỉ học lúc IDLE và khi mẫu không vượt ngưỡng. Trong EVENT thì đóng băng nền. (Bản code
dùng trung bình mũ thay cho trung vị 10 phút, để trạng thái lưu được gọn vào JSON.) Có thêm lệnh `/calibrate` (và nút trên web) để chốt nền ngay lúc
đặt trạm lên bàn demo.

**Bộ phát hiện sự kiện** (máy trạng thái, luật tất định):
- IDLE → EVENT khi trong 3 giây liên tiếp PM2.5 > nền + max(15, 20 % nền), hoặc độ ẩm tăng > 3 %
  trong 20 giây.
- EVENT → COOLDOWN khi PM2.5 về dưới nền + 10 % và độ ẩm về dưới nền + 2 %, liên tục 20 giây.
- COOLDOWN → IDLE sau 20 giây, nhãn trở về `normal`.

**Phân loại.** Bắt đầu đoán từ giây thứ 8 của sự kiện, cập nhật mỗi giây tới giây thứ 30, sau đó
khoá nhãn tới hết sự kiện. Nhãn hiện ra trong **≤ 10 s** thay vì 15–111 s, và không nhấp nháy giữa
chừng.

**8 đặc trưng của sự kiện**, tính trên phần vượt nền kể từ lúc bắt đầu. Ít đặc trưng, đều có ý nghĩa
vật lý và giải thích được:

| Đặc trưng | Tách được |
| --------- | --------- |
| `rise_rate`: tốc độ tăng PM2.5 lớn nhất trong 5 s, chia cho nền | khói (rất nhanh) với hơi nước (chậm) |
| `peak_rel`: đỉnh vượt nền / nền | độ mạnh |
| `ex_r1`: ΔPM1 / ΔPM2.5 | khói mịn với bụi thô |
| `ex_r10`: ΔPM10 / ΔPM2.5 | bụi thô |
| `coarse_frac`: Δ(n50 + n100) / Δn03 | bụi phấn (dùng kênh đếm hạt, lần đầu được dùng) |
| `d_hum`: độ ẩm lớn nhất − nền | hơi nước (nấu ăn) |
| `d_temp`: nhiệt độ lớn nhất − nền | hơi nóng |
| `spikiness`: số đỉnh / thời lượng | nhang (nhiều gai) với bụi (một đợt) |

8 số này là tóm tắt, nên làm mất **hình dạng** đường cong: lên nhanh hay chậm, mấy gai, giảm ra
sao. Phân loại chỉ trên chúng thì gần như là đặt vài ngưỡng. Vì vậy mô hình chính dùng cả cửa sổ:

**Mô hình chính: kNN-DTW trên đường cong sự kiện** (`aiot.source_model.ShapeKnnModel`).
- Đầu vào là cả cửa sổ từ **10 s trước lúc vượt ngưỡng tới giây hiện tại** (tối đa 40 hàng, mỗi giây
  một hàng), 6 kênh so với nền: PM1, PM2.5, PM10, đếm hạt thô (log của phần vượt nền tương đối), Δđộ
  ẩm, Δnhiệt độ (`aiot.source_event.shape`).
- Khoảng cách DTW nhiều kênh, cửa sổ ±5 s. DTW chịu được chuyện cùng một đạo cụ nhưng đỉnh tới sớm
  hay muộn vài giây, mà vẫn phân biệt được hình dạng (một đợt êm với nhiều gai). 1-NN DTW là đối
  chứng kinh điển khó thắng khi phân loại chuỗi thời gian với ít mẫu.
- **Phân loại sớm:** ở giây thứ t của sự kiện, chỉ so t giây đầu của các mẫu học.
- Mỗi lần làm đạo cụ là **một mẫu học** (cả đường cong), không cắt thành nhiều cửa sổ gần trùng.
  Mô hình là bảng đường cong (JSON), không cần scikit-learn trên Pi. Một lần đoán với 60 mẫu mất
  khoảng 21 ms trên laptop, ước chừng 100 ms trên Pi 4.
- Mẫu gần nhất xa hơn ngưỡng (1,5 × p95 khoảng cách khi đánh giá chéo) thì hiện "Chưa gặp kiểu
  này" và quay về luật ngưỡng. Như vậy hệ thống tự biết khi nào nó không chắc.

**Đối chứng** (cùng đánh giá leave-one-event-out trong `training/train_demo.py`): kNN (k = 3) trên 8
số tóm tắt, luật viết tay, cây quyết định độ sâu 3, và RandomForest cũ (mô phỏng). Hai câu hỏi cần
dữ liệu thật trả lời: *đường cong có hơn tóm tắt không?* và *dữ liệu thật ít có hơn dữ liệu mô phỏng
nhiều không?*. Kết quả nào cũng báo cáo trung thực. 8 số tóm tắt vẫn giữ để làm thẻ giải thích.

## 4. Phần "trông đẹp" khi demo

0. **Đường cong chồng lên mẫu gần nhất**: đường đang đo (đậm) vẽ chồng lên đường cong của mẫu học
   gần nhất (nhạt), cùng tô màu nhãn. Đây là cách trực quan nhất để thấy kNN-DTW đang so cái gì.
1. **Bản đồ sự kiện 2D** trên web (`cloud/`): trục x = `d_hum`, trục y = `ex_r10` (hoặc 2 thành phần
   PCA). Các mẫu đã dạy là các đám màu theo nhãn. Khi có sự kiện, **một chấm mới chạy dần vào một
   đám**, cập nhật mỗi giây. Người xem *thấy* AI đang so sánh.
2. **Thẻ giải thích** dưới nhãn: *"Nấu ăn (3/3 mẫu gần nhất) · Độ ẩm +11 % · hạt mịn ít · tăng chậm"*.
   Lấy từ 2 đặc trưng lệch khỏi nền nhiều nhất và láng giềng gần nhất.
3. **Dạy trực tiếp trên sân khấu** (`/teach cooking` hoặc nút trên web): sự kiện tiếp theo được lưu
   thành mẫu mới, và kNN có hiệu lực ngay, không cần huấn luyện lại. Màn demo: *"Trạm chưa từng thấy
   bột mì → báo 'chưa gặp' → dạy 1 lần → lần sau nhận ra."*
4. **Đếm ngược "sạch lại sau X phút"**: khớp hàm mũ `C(t) = nền + A·e^(−kt)` cho đoạn suy giảm. `k`
   lúc quạt bật và lúc quạt tắt đo riêng trong buổi thu dữ liệu, nên cùng thể hiện được tác dụng của
   quạt: *"quạt bật: 3 phút · không quạt: 9 phút"*. Mô hình này chỉ đúng cho đúng phòng đó, và với demo
   thì như vậy là đủ.
5. **Telegram** gửi một tin cho mỗi sự kiện, kèm ảnh camera và biểu đồ 2 phút có tô màu sự kiện.

## 5. Kế hoạch thu dữ liệu (một buổi khoảng 1,5 giờ, tại đúng chỗ sẽ demo)

0. Kiểm tra mẫu 1 giây đang ghi (lệnh ở `docs/nhan_dien_nguon_bui.md`, mục "Việc tiếp theo"). Đặt
   cảm biến đúng vị trí và hướng sẽ dùng khi demo, rồi không xê dịch nữa.
1. **Thu thử 15 phút**: mỗi đạo cụ làm 1 lần, xem đồ thị. Nếu hai đạo cụ trông giống nhau thì đổi
   đạo cụ ngay tại chỗ.
2. **Thu chính**: mỗi đạo cụ 12 lần, thứ tự xen kẽ ngẫu nhiên, mỗi lần 30–60 s. Giữa các lần bật quạt
   cho về nền. Gán nhãn bằng `scripts/label_event.py` (hoặc thêm `--now` để đánh dấu bắt đầu/kết thúc
   bằng phím Enter).
3. **20 phút "bình thường có nhiễu"**: đi lại, đứng sát, thở gần, bật/tắt đèn, che cảm biến ánh sáng.
   Phần này để bộ phát hiện không báo oan.
4. **6 lần suy giảm** (3 lần có quạt, 3 lần không) để đo `k` cho đếm ngược.
5. Hôm demo: tới sớm 10 phút, `/calibrate`, rồi làm mỗi đạo cụ 1 lần với `/teach` để thêm mẫu ngày
   hôm đó. Đây là "hiệu chuẩn tại chỗ", nói thẳng như vậy khi trình bày.

An toàn: nhang/diêm làm theo quy định hiện có (lượng nhỏ, có người trông, nơi thoáng). Cốc nước nóng
đặt **dưới và lệch** cảm biến, mỗi lần ≤ 30 s, để hơi nước không đọng trong buồng đo PMS7003. Bụi
phấn/bột chỉ một nhúm, cách 50 cm, để không làm bẩn quạt hút của cảm biến.

## 6. Thay đổi code

| File | Việc | Công |
| ---- | ---- | ---- |
| `aiot/source_event.py` (mới) | Nền phòng + máy trạng thái sự kiện + 8 đặc trưng; hàm thuần, có test | Vừa |
| `aiot/source_model.py` | Thêm định dạng `aiot-knn-v1` (mẫu + trung bình/độ lệch chuẩn hoá), `predict` trả nhãn, phiếu, khoảng cách, láng giềng; vẫn đọc được RF cũ | Thấp |
| `aiot/controller.py`, `config.yaml` | `source.profile: demo \| real`. `demo` dùng pipeline sự kiện, `min_state_s` 20 s, bỏ ngưỡng tuyệt đối 55,5 cho `pollution`; `real` giữ nguyên như hiện tại | Thấp |
| `training/train_demo.py` (mới) | Đọc `fast_readings` + `source_labels` thật, tách sự kiện, đánh giá leave-one-session-out, so với 3 đối chứng, xuất `models/source_knn.json` và ảnh bản đồ 2D | Vừa |
| `aiot/commands.py`, `aiot/bot.py` | `/calibrate`, `/teach <nhãn>` (đi qua khoá state giống lệnh quạt) | Thấp |
| `cloud/` | Bản đồ sự kiện 2D, thẻ giải thích, đếm ngược; uplink gửi thêm đặc trưng sự kiện (không có dữ liệu cá nhân) | Vừa |
| `scripts/inject_pm.py` | Kịch bản phát lại từ **sự kiện thật đã thu** thay vì mô phỏng, để tập dượt khi không có đạo cụ | Thấp |

Ước lượng: 1 buổi thu dữ liệu + 1–2 ngày code + 1 buổi tập dượt.

## 7. Phải nói thật trong báo cáo

- Mô hình demo được **chủ ý tinh chỉnh cho một phòng, một cảm biến và 4 đạo cụ**. Nó không phải bộ
  nhận diện nguồn bụi tổng quát cho nhà thật. Đạo cụ là chất thay thế: hơi nước thay cho nấu ăn, bụi
  phấn thay cho bụi ngoài trời, nhang thay cho khói thuốc.
- Con số độ chính xác lấy theo leave-one-session-out trên dữ liệu thật, không lấy trên mô phỏng.
- Nền "bình thường" là nền của phòng (có thể đã 150+ µg/m³), không phải "không khí tốt". Mức AQI
  thật vẫn hiển thị riêng và vẫn theo US EPA.
- Mô hình RF tổng quát (bản hiện tại, F1 0,93 trên mô phỏng) vẫn giữ trong báo cáo làm hướng phát
  triển, cùng với các giới hạn đã ghi ở `docs/nhan_dien_nguon_bui.md`.

## 8. Thứ tự làm đề xuất

1. Bật lại dịch vụ, kiểm tra mẫu 1 giây, thu thử 15 phút với 4 đạo cụ. **Đây là điểm quyết định**:
   nếu 4 đạo cụ không tách được trên đồ thị thì chọn lại đạo cụ trước khi viết code.
2. `source_event.py` + `train_demo.py` + kNN, chạy trên dữ liệu thu chính.
3. Profile `demo` trong controller, `/calibrate`, `/teach`.
4. Bản đồ 2D, thẻ giải thích, đếm ngược trên web.
5. Tập dượt kịch bản: bình thường → nước nóng (quạt bật) → về nền, đếm ngược → nhang (còi) → phấn
   (quạt + còi) → dạy một đạo cụ mới.
