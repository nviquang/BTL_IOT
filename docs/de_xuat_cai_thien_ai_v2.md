# Đề xuất cải thiện AI — bản 2: các ý tưởng "đáng kể lại"

Ngày 11/11/2026. Bổ sung cho `docs/de_xuat_cai_thien_ai.md`. Bản 1 đúng hướng (fusion, nhận diện
nguồn, hiệu chỉnh, mô hình phòng), nhưng khi trình bày vẫn nghe như *"trạm đo thông minh hơn một
chút"*. Bản này tìm những ý tưởng có **câu chuyện mạnh hơn**: gắn với một vấn đề thật người nghe
đã biết, cho ra một con số dễ nhớ, hoặc giải được một điểm yếu mà giảng viên sẽ hỏi.

Tiêu chí chọn: (1) một câu là người ngoài ngành hiểu vì sao cần; (2) demo được trong ≤ 2 phút;
(3) dùng được phần cứng hiện có; (4) có nguồn tham khảo để không bị bắt bẻ; (5) giữ nguyên nguyên tắc
local-first và "Gemini không điều khiển thiết bị".

## Tóm tắt

| #   | Ý tưởng                                                              | Câu "móc" khi trình bày                                                        | Công   | Mức     |
| --- | -------------------------------------------------------------------- | ------------------------------------------------------------------------------ | ------ | ------- |
| 1   | Phân biệt **khói bếp** và **khói cháy** (mở rộng ý A của bản 1)      | "Báo cháy thật kêu oan khi rán cá. Trạm của em biết đâu là bếp, đâu là cháy."  | Vừa    | **Làm** |
| 2   | **Sổ phơi nhiễm** + "quạt đã giúp bạn tránh bao nhiêu"               | "Hôm nay cả nhà hít tương đương 0,6 điếu thuốc; quạt giúp tránh 0,2 điếu."     | Thấp   | **Làm** |
| 3   | **Mô hình nền tảng** (Chronos-Bolt) dự báo không cần huấn luyện + **khoảng tin cậy** | "Ngày đầu cắm điện đã dự báo được, và nói luôn mình chắc đến đâu."             | Thấp–vừa | **Làm** |
| 4   | **Tự kiểm tra quạt lọc**: đo CADR, phát hiện lọc bẩn, nhắc thay      | "Máy lọc tự biết khi nào nó yếu đi."                                           | Thấp   | **Làm** |
| 5   | Trạm **biết nói** tiếng Việt + bản tin sáng sinh offline bằng LLM nhỏ | "Mất Internet vẫn có bản tin sáng; có khói thì trạm lên tiếng."                | Vừa    | Nên     |
| 6   | So với **trạm hàng xóm** (Sensor.Community, Open-Meteo): ô nhiễm do nhà hay do phố | "Bụi này do nhà mình hay cả khu? Đóng cửa hay mở cửa?"                         | Thấp   | Nên     |
| 7   | **Nhật ký nhà** dạng ảnh: biểu đồ ngày có chú thích sự kiện          | Một hình gửi Telegram mỗi sáng, ai nhìn cũng hiểu                              | Thấp   | Nên     |
| 8   | **Bản sao số** (digital twin) + MPC có "lá chắn an toàn"             | "AI đề xuất lịch chạy quạt, luật an toàn duyệt rồi mới cho chạy."              | Cao    | Tuỳ     |

Gợi ý gói tối thiểu nếu thời gian ít: **1 + 2 + 3**. Ba ý này cho ba câu chuyện khác nhau (an toàn,
sức khoẻ, dự báo trung thực) và đều dựa trên bảng `features_1m` + mô hình phòng của bản 1.

---

## 1. Khói bếp hay khói cháy — biến "nhận diện nguồn" thành bài toán an toàn

**Vấn đề thật:** báo cháy gia đình kêu nhầm khi nấu ăn nhiều đến mức người dùng tháo pin, rồi
thiết bị vô dụng khi có cháy thật. Tiêu chuẩn **UL 217 bản 8** (Mỹ) vì thế thêm một phép thử: nướng
hai miếng thịt cách báo cháy ~3 m, **không được kêu**; và một phép thử đốt xốp polyurethane, **phải
kêu**. Theo NIST, khi tiêu chuẩn mới ra, trong 45 mẫu báo cháy đang bán không mẫu nào đạt mức yêu cầu.
Nói cách khác: *phân biệt khói bếp với khói cháy là bài toán ngành công nghiệp đang giải*, không phải
bài tập tự nghĩ ra.

**Khác gì ý A của bản 1:** bản 1 phân loại 6 nguồn ngang hàng. Ở đây đặt **một câu hỏi có hậu quả**:
*"Có phải cháy không?"*, với hai lớp chính và chi phí sai lệch không đều (bỏ sót cháy tệ hơn nhiều so
với báo nhầm). Các lớp khác (hương, bụi, hoá chất) vẫn giữ làm lớp phụ.

**Đặc trưng có cơ sở vật lý** (dùng lại vector 1 phút của bản 1):

- Tốc độ tăng PM1 và PM2.5, độ cong (tăng đều hay tăng tốc). Cháy âm ỉ tăng chậm nhưng không dừng;
  nấu ăn tăng theo đợt rồi giảm khi tắt bếp.
- Tỉ lệ PM1/PM2.5 (khói đốt rất mịn).
- MQ135 bật trước hay sau PM.
- Δnhiệt độ, Δđộ ẩm (nấu nước/xào làm tăng ẩm; giấy cháy thì không).
- Ngữ cảnh: giờ ăn, đèn bếp (lux), có người không (YOLO).

**Mô hình và đánh giá:**

- Random Forest/LightGBM như bản 1, nhưng **chọn ngưỡng theo chi phí**: cố định độ nhạy với lớp
  "cháy" ≥ 95% trên tập test rồi mới tối ưu tỉ lệ báo nhầm. Báo cáo đường ROC/PR và điểm vận hành.
- Đối chứng: "báo cháy ngây thơ" = PM2.5 vượt một ngưỡng cố định. Slide chính: *cùng độ nhạy, mô hình
  fusion giảm báo nhầm khi nấu ăn từ X% xuống Y%*.
- Có cơ sở tham khảo cho phần ML: một nghiên cứu dùng Random Forest nhận diện 442 sự kiện phát thải
  trong 18 căn hộ ở California (hút thuốc, nấu ăn, dọn dẹp, hương, nến) từ cảm biến giá rẻ; nghiên cứu
  AS-LUNG ở Đài Loan đo PM1 + PM2.5 trong nhà và thấy đốt hương cho mức tăng PM2.5 5 phút cao nhất
  trong các nguồn đơn lẻ.

**Thí nghiệm lấy nhãn** (lượng nhỏ, nơi thoáng, có người trông, có bình nước — theo quy định an toàn
hiện có): "cháy" = mẩu giấy nhỏ, sợi bông, que diêm trong khay kim loại; "bếp" = đun nước, rán trứng,
nướng bánh mì; mỗi loại 8–10 phiên, chia train/test theo phiên.

**Ứng dụng:** "khói cháy" → cảnh báo Telegram ưu tiên cao kể cả giờ yên lặng, (tuỳ chọn) trạm nói to
(ý 5); "khói bếp" → bật quạt, tin nhắn nhẹ nhàng, *không* làm ồn. Đây là quyết định do luật tất định
đưa ra dựa trên nhãn, giống cách MQ135 đang làm.

**Phải nói thật:** đây là **proof of concept, không thay báo cháy đạt chuẩn**. Thí nghiệm nhỏ trong
hộp, không phải phòng thử UL; PMS7003 có quạt hút nên phản ứng khác cảm biến quang điện của báo cháy.

## 2. Sổ phơi nhiễm và "giá trị của AI" tính bằng điếu thuốc

**Vấn đề với cách báo hiện tại:** "PM2.5 = 48 µg/m³" không làm ai thay đổi hành vi. Một con số quy
đổi mà ai cũng cảm được thì có.

**Làm gì:**

1. **Liều phơi nhiễm** mỗi ngày = tích phân PM2.5 (đã hiệu chỉnh theo ý B của bản 1) theo thời gian
   người ở nhà (dùng lux/YOLO/giờ ngủ để biết có người, mặc định ở nhà cả ngày).
2. **Quy đổi điếu thuốc** theo quy tắc của Berkeley Earth: PM2.5 trung bình 24 giờ khoảng
   **22 µg/m³ ≈ 1 điếu thuốc/ngày**. Ghi rõ đây là quy đổi theo *rủi ro tử vong* ước lượng, không phải
   khói thuốc và bụi giống nhau về hoá học, và có nhà nghiên cứu ước lượng thấp hơn nhiều lần.
3. **Phản thực tế (counterfactual):** dùng mô hình phòng ở ý C của bản 1 (`k_tự_nhiên`, `k_quạt` đã
   fit) mô phỏng lại ngày hôm đó *nếu quạt không bật* → chênh lệch liều = **phần quạt và luật điều
   khiển đã giúp tránh**. Cùng cách này so được "luật hiện tại" với "luật có dự báo chủ động".

**Vì sao hấp dẫn:** đây là chỗ AI/mô hình **tạo ra giá trị đo được bằng một con số**, đúng điều giảng
viên chê là thiếu. Báo cáo sáng có một dòng: *"Hôm qua: 0,6 điếu · quạt giúp tránh 0,2 điếu ·
nguồn chính: bếp (3 lần)"*. Báo cáo tuần có biểu đồ cột theo ngày.

**Công:** thấp — chỉ là tính toán trên dữ liệu đã có, cộng mô phỏng ODE một biến.

## 3. Dự báo: mô hình nền tảng zero-shot + khoảng tin cậy

Dự báo theo giờ cần được đối chiếu với số đo của trạm. Một buổi demo ngắn chỉ cho thấy
hệ thống tạo dự báo, còn độ chính xác cần được đo trên nhiều giờ.

**3a. Mô hình nền tảng chuỗi thời gian (time-series foundation model).** Các mô hình như
**Chronos-Bolt** (Amazon), TimesFM (Google), Moirai được huấn luyện sẵn trên rất nhiều chuỗi thời gian
và dự báo **không cần huấn luyện lại** (zero-shot). Chronos-Bolt bản *tiny* chỉ **~9 triệu tham số**,
giấy phép Apache-2.0, chạy được trên CPU — nhỏ hơn cả YOLOv8n mà nhóm từng chạy trên Pi. Năm 2026 đã
có nghiên cứu (ĐH Modena và Reggio Emilia) benchmark đúng bài toán *dự báo PM2.5 từ cảm biến giá rẻ
bằng mô hình nền tảng*, so zero-shot, fine-tune và mô hình học sâu tự huấn luyện.

Ngày đầu cắm điện, dữ liệu còn ít. Khi có số đo của trạm, có thể so sánh ba phương pháp:
persistence, seasonal naive và Chronos-Bolt zero-shot.

Cần đo trước: thời gian suy luận và RAM trên Pi 4 (chưa thấy số liệu công khai). Nếu `torch` nặng, xuất
ONNX. Chạy mỗi giờ nên vài giây cũng chấp nhận được.

**3b. Khoảng dự báo bằng conformal prediction.** Lấy sai số dự báo trong cửa sổ trượt gần đây (ví dụ
14 ngày) theo từng horizon, lấy phân vị 90% → dự báo trở thành *"PM2.5 lúc 20h: 42 (31–60) µg/m³"*.
Phương pháp không cần giả định phân phối, vài chục dòng code, áp dụng cho mọi mô hình ở 3a. Đã có bài
báo kết hợp conformal với dữ liệu cảm biến giá rẻ (arXiv 2404.05482), và dùng cửa sổ hiệu chỉnh trượt
cho dữ liệu thời gian (arXiv 2605.03795). Chronos-Bolt vốn cho ra phân vị, dùng để so.

Ứng dụng trực tiếp:

- Tin dự báo nói **xác suất đổi nhóm AQI**: *"70% khả năng sang mức Kém trước 20h"* — dễ hiểu hơn MAE.
- Luật chủ động (bật quạt trước) dùng **cận dưới** của khoảng thay vì giá trị giữa → ít bật oan.
- Slide đánh giá: **độ phủ thực tế** của khoảng 90% (nếu ~90% là mô hình "trung thực về độ chắc chắn").

## 4. Quạt lọc tự kiểm tra: đo CADR, phát hiện lọc bẩn

**Vấn đề thật:** người dùng máy lọc không biết lọc còn tốt không, thường thay theo lịch hoặc không
bao giờ thay.

**Làm gì:**

1. Mở rộng ý C của bản 1: tự tìm các đoạn PM suy giảm (sau nấu ăn, hương…) khi quạt bật và khi tắt,
   fit `ln(C)` theo thời gian để có tốc độ suy giảm. Đại học Toronto có phương pháp **tự động tách đoạn
   suy giảm** từ chuỗi liên tục của một cảm biến giá rẻ (phân cụm k-means/DBSCAN rồi cân bằng khối
   lượng), đúng thứ nhóm cần.
2. **CADR = thể tích phòng/hộp × (k_quạt_bật − k_quạt_tắt)** — công thức chuẩn trong các nghiên cứu
   đánh giá máy lọc. Nghiên cứu máy lọc tự chế Corsi-Rosenthal cho thấy cảm biến giá rẻ cho CADR gần với
   thiết bị nghiên cứu.
3. **Chế độ tự kiểm tra chủ động** (lệnh `/selftest`, chạy khi phòng trống): chờ một sự kiện bụi, rồi
   xen kẽ quạt bật/tắt theo lịch để đo cả hai `k` trong cùng điều kiện.
4. Theo dõi CADR theo tuần → CADR giảm dần = lọc bẩn → nhắc thay lọc (**bảo trì dự đoán**). Demo được
   bằng cách che một nửa cửa hút của quạt mini bằng giấy: CADR đo được giảm rõ.

**Vì sao hấp dẫn:** biến dòng "Hiệu quả lọc — có khác biệt đo được" trong bảng nghiệm thu thành
**một con số có đơn vị** (m³/h), và cho thêm một tính năng sản phẩm mà máy lọc rẻ không có.

## 5. Trạm biết nói, và vẫn có bản tin khi mất Internet

**5a. Giọng nói tiếng Việt offline.** **Piper TTS** chạy cục bộ, có bản build arm64 cho Pi 4 và có giọng
`vi_VN`. Cảnh báo quan trọng (khói cháy, MQ135, quạt bật/tắt) được đọc to qua loa USB: *"Phát hiện
khói bếp, đã bật quạt lọc, dự kiến sạch sau 15 phút."* Trong phòng thuyết trình, một thiết bị **tự lên
tiếng** gây ấn tượng hơn mọi slide. Cần mua thêm loa USB/3,5 mm nhỏ; kiểm tra giấy phép từng giọng
trong MODEL_CARD.

**5b. LLM nhỏ chạy trên Pi cho bản tin sáng.** Nguyên tắc local-first đang có một chỗ hở: lời khuyên chỉ
có khi Gemini gọi được, nếu không thì dùng mẫu cố định. Một LLM nhỏ (Gemma 3 1B, Qwen 2.5 1.5B, dạng
GGUF 4-bit qua `llama.cpp`) trên Pi 4 chỉ đạt khoảng **vài token/giây** (các số đo công khai dao động
2–9 tok/s, độ tin cậy thấp) — quá chậm để chat, nhưng **đủ cho việc chạy nền**: bắt đầu sinh bản tin
lúc 05:45, xong trước 06:30. Thứ tự ưu tiên thành *Gemini → LLM cục bộ → mẫu cố định*. Cần thử chất
lượng tiếng Việt trước (một số model nhỏ hỗ trợ tiếng Việt kém) và đo RAM khi chạy cạnh collector.

Ghép với ý E (trợ lý hỏi-đáp) của bản 1: câu hỏi tự do vẫn đi Gemini; chỉ khi mất mạng mới trả lời
ngắn bằng LLM cục bộ.

## 6. So với trạm hàng xóm: ô nhiễm do nhà mình hay do cả phố

**Làm gì:** lấy PM2.5 ngoài trời từ hai nguồn: Open-Meteo Air Quality (mô hình CAMS, đã dự kiến) và
các cảm biến **Sensor.Community** gần nhà (mạng cảm biến công dân, có API mở; đo thật thay vì mô hình).
Tính **tỉ lệ trong/ngoài (I/O)** theo thời gian thực:

- I/O ≫ 1 và tăng đột ngột → **nguồn trong nhà** (khớp với ý 1); một nghiên cứu PurpleAir ở 18 căn hộ
  ghi nhận I/O trung bình ~14 khi nấu ăn.
- I/O ≈ 1 và cả khu cùng tăng → **ô nhiễm khu vực** → khuyên đóng cửa, bật quạt chủ động.
- Trong bẩn, ngoài sạch → khuyên mở cửa sổ (ý C bản 1, nay có số đo thật).

Công thấp (một hàm gọi API mỗi 5–10 phút, lưu vào `features_1m`), và là một ví dụ fusion "trong nhà +
ngoài trời" rất dễ vẽ. Lưu ý: Sensor.Community chủ yếu nhận cảm biến **đặt ngoài trời**, nên nhóm chỉ
*đọc* dữ liệu, không đẩy dữ liệu trạm trong nhà lên mạng đó. Cần kiểm tra mật độ cảm biến ở khu vực
trước khi dựa vào.

## 7. Nhật ký nhà dạng ảnh

Mỗi sáng bot gửi **một ảnh** (matplotlib, sinh trên Pi): đường PM2.5 24 giờ, nền tô màu theo nhóm AQI,
dải màu khi quạt bật, và nhãn sự kiện mà ý 1 nhận diện ("18:40 nấu ăn", "21:10 hương"), kèm dòng sổ phơi
nhiễm của ý 2. Đây là cách rẻ nhất để mọi chức năng AI **hiện ra cùng một chỗ** — chính là lời đáp cho
nhận xét "các cảm biến rời rạc". Dùng lại cho slide và dashboard.

## 8. Bản sao số + MPC có lá chắn an toàn (chỉ làm nếu dư thời gian)

**Ý tưởng:** mô hình phòng (ý C bản 1) + dự báo (ý 3) thành một **bản sao số** chạy trên dashboard với
thanh trượt "nếu bật quạt bây giờ thì…". Một bộ điều khiển dự báo (MPC) hoặc tác tử học tăng cường
(RL, huấn luyện trong bản sao số) *đề xuất* lịch chạy quạt tối ưu giữa phơi nhiễm, điện năng và tiếng
ồn giờ ngủ; luật tất định hiện có đóng vai **lá chắn an toàn**: đề xuất nào vi phạm luật (ví dụ tắt quạt
khi MQ135 đang báo) thì bị bác. Cách này cho AI điều khiển một phần mà vẫn giữ nguyên tắc an toàn.

**Vì sao xếp cuối, và điều cần nói thật:** đã có nghiên cứu RL cho máy lọc báo tiết kiệm ~40% điện
năng, nhưng chủ yếu trong mô phỏng; và một nghiên cứu của ĐH Toronto thấy **điều khiển theo ngưỡng đã
gần tối ưu, MPC chỉ cải thiện chút ít**. Nếu làm, hãy trình bày như một *thí nghiệm có câu trả lời
trung thực* ("AI có đáng không?") trên bản sao số, dùng ý 2 để chấm điểm, thay vì hứa hẹn cải thiện lớn.

---

## Kịch bản demo mới (≈ 6 phút)

1. **Mở đầu bằng vấn đề**, không bằng phần cứng: *"Báo cháy kêu oan khi nấu ăn, máy lọc không biết
   khi nào yếu, và con số PM2.5 không ai hiểu."*
2. Đun chảo/nướng bánh mì cạnh hộp cảm biến → dashboard: *"Khói bếp (88%) — không phải cháy"*; quạt
   bật; trạm nói nhẹ; Telegram nhận tin kèm thời gian về sạch.
3. Đốt một mẩu giấy nhỏ trong khay kim loại (có người trông) → *"Nghi khói cháy"* → cảnh báo ưu tiên
   cao, trạm nói to. Slide: cùng độ nhạy, báo nhầm giảm X → Y so với ngưỡng cố định.
4. Che nửa cửa hút quạt → `/selftest` → CADR giảm, bot nhắc kiểm tra lọc.
5. `/forecast` → dự báo Chronos-Bolt kèm khoảng tin cậy và xác suất đổi nhóm AQI; slide bảng so sánh
   4 mô hình và độ phủ của khoảng 90%.
6. Ảnh "nhật ký nhà" + sổ phơi nhiễm: *"Hôm qua 0,6 điếu, quạt giúp tránh 0,2 điếu."*
7. Ngắt Wi-Fi Pi → `/report` vẫn có bản tin do LLM cục bộ viết (bot gửi bù khi có mạng lại) →
   chứng minh local-first.

## Lộ trình gắn với bản 1

| Giai đoạn | Việc (bản 1 + bản 2)                                                                                     | Ra được gì                                         |
| --------- | -------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| 1         | `features_1m` (bản 1); fit suy giảm + CADR (ý 4); sổ phơi nhiễm + phản thực tế (ý 2); ảnh nhật ký (ý 7)  | Con số CADR, "quạt giúp tránh N điếu", ảnh mỗi sáng |
| 2         | Thí nghiệm bếp/cháy + Random Forest có ngưỡng theo chi phí (ý 1); Chronos-Bolt + conformal (ý 3)         | Demo bếp/cháy, bảng 4 mô hình dự báo               |
| 3         | Piper + LLM cục bộ (ý 5); dữ liệu hàng xóm I/O (ý 6); Gemini function calling (bản 1, ý E)               | Trạm biết nói, bản tin offline, khuyên đóng/mở cửa |
| 4 (tuỳ)   | Bản sao số + MPC/RL có lá chắn (ý 8)                                                                     | Thí nghiệm "AI có đáng không?"                     |

## Việc cần kiểm tra trước khi chốt

- Đo Chronos-Bolt tiny trên Pi 4: thời gian, RAM, có cần ONNX không.
- Chạy thử một LLM 1–1,5B trên Pi 4 với prompt bản tin tiếng Việt: tốc độ, RAM, chất lượng câu.
- Chọn giọng Piper `vi_VN` và kiểm tra giấy phép; mua loa nhỏ.
- Kiểm tra có cảm biến Sensor.Community nào đủ gần không (API `data.sensor.community`).
- Đọc bản đầy đủ của nghiên cứu Modena (foundation model + cảm biến giá rẻ) và Tang et al. (Random
  Forest sự kiện trong nhà) để trích kết quả chính xác vào báo cáo.

## Nguồn

- UL Standards & Engagement — giảm báo nhầm khi nấu ăn với UL 217:
  https://ulse.org/standards-and-engagement/standards-matter/helping-reduce-cooking-nuisance-alarms-ul-217
- NIST — thử nghiệm báo cháy theo tiêu chuẩn mới: https://www.nist.gov/node/1130736 ;
  báo cáo cho CPSC (2017): https://www.cpsc.gov/s3fs-public/NIST%20Smoke%20Alarm%20Contractor%20Report%206b6%20FINAL%20February%202017.pdf
- Berkeley Earth — Air Pollution and Cigarette Equivalence:
  https://berkeleyearth.org/air-pollution-and-cigarette-equivalence
- Chronos-Bolt tiny (model card): https://huggingface.co/amazon/chronos-bolt-tiny ;
  AWS — zero-shot forecasting với Chronos-Bolt:
  https://aws.amazon.com/jp/blogs/news/machine-learning-fast-and-accurate-zero-shot-forecasting-with-chronos-bolt-and-autogluon
- Benchmark foundation model cho PM2.5 từ cảm biến giá rẻ (Expert Systems with Applications, 2026):
  https://iris.unimore.it/retrieve/8793bb55-e969-44bf-9940-23271e209201/1-s2.0-S0957417426020105-main.pdf
- Foundation model với PM2.5 cháy rừng California: https://arxiv.org/abs/2607.07951
- Conformal prediction + cảm biến giá rẻ: https://arxiv.org/html/2404.05482v1 ;
  conformal với cửa sổ trượt (GraphSVR): https://arxiv.org/pdf/2605.03795
- Lung et al., AS-LUNG trong nhà (Indoor Air): https://www.pubmed.gov/33047373/
- Random Forest nhận diện sự kiện phát thải trong nhà; I/O PurpleAir 18 căn hộ (UCLA):
  https://escholarship.org/content/qt9vc7k29j/qt9vc7k29j.pdf ; https://www.escholarship.org/uc/item/2h03q3kw
- CADR từ tốc độ suy giảm với cảm biến giá rẻ: https://pmc.ncbi.nlm.nih.gov/articles/PMC10339722 ;
  https://www.pdx.edu/research-experience/sites/g/files/znldhr3146/files/2022-11/Baorong%20Luo%27s%20REU%20presentation.pdf
- ĐH Toronto — tách đoạn suy giảm bằng phân cụm: https://hab.civmin.utoronto.ca/wp-content/uploads/2024/06/Clustering_ACH_1pg.pdf ;
  điều khiển dự báo vs ngưỡng: https://hab.civmin.utoronto.ca/wp-content/uploads/2025/06/forecasting_control.pdf
- RL cho máy lọc không khí (Building and Environment, 2023):
  https://www.aminer.org/pub/648d4630d68f896efac47f9c/developing-smart-air-purifier-control-strategies-for-better-iaq-and-energy-efficiency
- Piper TTS: https://github.com/rhasspy/piper
- Sensor.Community API (ví dụ đẩy/đọc dữ liệu): https://www.hackster.io/laurens-slats/air-quality-sensing-w-blues-qubitro-sensor-community-5fcaa1

Số liệu tốc độ LLM trên Pi 4 lấy từ các trang tổng hợp không rõ phương pháp, chỉ dùng để ước lượng thô;
phải tự đo trước khi đưa vào báo cáo. Chưa đọc toàn văn các bài báo Modena và Tang et al.; kết quả cụ
thể của họ chưa được trích trong tài liệu này.
