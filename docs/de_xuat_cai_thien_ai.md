# Đề xuất cải thiện phần AI và gắn kết cảm biến

Ngày 11/11/2026. Viết sau nhận xét của giảng viên: *phần AI là quan trọng nhất nhưng chưa có tính
ứng dụng cao, chưa hấp dẫn khi trình bày; các cảm biến đang rời rạc từng module.*

## 1. Chẩn đoán: vì sao bị nhận xét như vậy

Nhìn vào code hiện tại, nhận xét là đúng:

| Thành phần                         | Đang làm gì                                                  | Vấn đề khi trình bày                                                                                                                                                                |
| ---------------------------------- | ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Gemini (`aiot/advice.py`)          | Viết `{headline, advice}` 1–3 lần/ngày                       | Là "trang trí chữ", người nghe không thấy AI hiểu gì.                                                                                                                               |
| YOLOv8n (`pi_detection_client.py`) | Đếm người, gửi ảnh qua MQTT                                  | Không ghi vào DB, không ảnh hưởng quyết định nào. Chạy riêng, tranh camera với `aiot/weather.py`.                                                                                   |
| Nắng/mưa (`aiot/weather.py`)       | Ngưỡng cứng lux + nhiệt độ/độ ẩm                             | Là luật if-else, không phải AI.                                                                                                                                                     |
| Quạt (`aiot/control.py`)           | Luật tất định theo PM2.5 + bit MQ135                         | Đúng hướng an toàn, nhưng chỉ nhìn PM2.5.                                                                                                                                           |

Về "rời rạc": mỗi cảm biến phục vụ đúng một chức năng — PMS7003 → quạt và dự báo, BH1750 → luật
nắng, DHT11 → luật mưa, MQ135 → một bit cảnh báo, camera → YOLO riêng.
Không có chỗ nào **kết hợp** các nguồn để rút ra điều mà một cảm biến đơn lẻ không biết.

**Mấu chốt:** hệ thống hiện *đo và báo số*, chưa *hiểu chuyện gì đang xảy ra trong nhà*. Hướng cải
thiện là đưa AI vào đúng chỗ đó, và dùng nó để nối các cảm biến lại.

## 2. Ý tưởng trung tâm: "Trạm hiểu ngữ cảnh"

Thay vì thêm nhiều mô hình rời nhau, xây **một lớp hợp nhất** (fusion) nhận mọi cảm biến, sinh ra
một *vector ngữ cảnh* mỗi phút, rồi các mô hình AI cùng dùng vector đó:

```
DHT11 ─┐                                   ┌─► (A) Nhận diện nguồn ô nhiễm  ──┐
BH1750 ┤                                   ├─► (B) Hiệu chỉnh PM theo độ ẩm   │
PMS7003┼─► aiot/features.py ─► features_1m ┼─► (C) Mô hình phòng: thời gian   ├─► quyết định quạt (luật)
MQ135 ─┤   (vector ngữ cảnh 1 phút)        │       về sạch, hiệu quả quạt      │   + Telegram + dashboard
Camera ┤                                   ├─► (D) Phát hiện lỗi cảm biến      │
(YOLO) ┘                                   └─► (E) Trợ lý Gemini hỏi-đáp ◄─────┘
Open-Meteo (ngoài trời) ───────────────────────┘
```

Câu chuyện khi trình bày đổi từ *"trạm đo PM2.5 và dự báo"* thành:

> "Trạm biết **trong nhà đang có chuyện gì** (đang nấu ăn, đốt hương, có khói thuốc, hay bụi từ
> ngoài vào), **biết quạt lọc cần bao lâu** để làm sạch, **tự biết khi cảm biến hỏng**, và trả lời
> câu hỏi bằng tiếng Việt."

Nguyên tắc cũ vẫn giữ: local-first, Gemini không điều khiển thiết bị, quạt vẫn theo luật tất định.
AI bổ sung *ngữ cảnh* cho luật và cho thông báo.

### Mỗi cảm biến phục vụ nhiều chức năng (dùng làm slide "trước/sau")

| Cảm biến      | Hiện tại               | Sau cải thiện                                                                                   |
| ------------- | ---------------------- | ----------------------------------------------------------------------------------------------- |
| PMS7003       | Quạt, dự báo           | A (tỉ lệ PM1/PM2.5/PM10, tốc độ tăng/giảm), B, C, D                                             |
| DHT11         | Luật mưa | A (nấu ăn làm tăng ẩm/nhiệt), B (hiệu chỉnh), D (kiểm tra chéo)                                 |
| BH1750        | Luật nắng              | A (đèn bật = có người, giờ sinh hoạt), D, ngữ cảnh ngày/đêm                                     |
| MQ135 DO      | 1 bit cảnh báo         | A (khí mà không có hạt bụi = cồn, sơn, gas), D (xác nhận sự kiện thật)                          |
| Camera + YOLO | Chạy riêng             | A (có người hay không), **nhãn tự động** cho mô hình, kiểm tra quyền riêng tư trước khi gửi ảnh |
| Open-Meteo    | Chưa dùng ở runtime    | C (so trong/ngoài nhà → khuyên mở hay đóng cửa sổ)                                              |

## 3. Các phương án, xếp theo mức đáng làm

### (A) Nhận diện sự kiện / nguồn ô nhiễm trong nhà — **ưu tiên số 1**

**Làm gì:** khi PM2.5 hoặc MQ135 bất thường, phân loại nguyên nhân: *nấu ăn*, *hương/nhang*,
*khói thuốc / đốt giấy*, *bụi (quét dọn, ngoài vào)*, *hơi hoá chất (cồn, xịt phòng)*, *không khí
ngoài trời xấu*.

**Vì sao hấp dẫn:** người nghe thấy ngay AI "hiểu" được điều cảm biến đơn lẻ không nói ra; demo
trực tiếp được trong 1–2 phút; và nó buộc phải dùng *tất cả* cảm biến.

**Đặc trưng (tính trên cửa sổ 1–5 phút quanh sự kiện):**

- PM: mức nền, đỉnh, tốc độ tăng (µg/m³/phút), thời gian lên đỉnh, hằng số suy giảm sau đỉnh;
  tỉ lệ PM1/PM2.5 và PM2.5/PM10 (khói đốt cho hạt rất mịn nên PM1 ≈ PM2.5; bụi thô kéo PM10 lên).
- MQ135: có bật bit trong cửa sổ không, bật trước hay sau khi PM tăng.
- DHT11: Δnhiệt độ, Δđộ ẩm so với 15 phút trước (nấu ăn thường đẩy cả hai lên).
- BH1750: mức lux, có thay đổi đột ngột không (bật đèn bếp).
- Camera: số người YOLO thấy (nếu bật).
- Ngữ cảnh: giờ trong ngày (sin/cos), quạt đang bật hay tắt, PM2.5 ngoài trời từ Open-Meteo.

**Mô hình:** Random Forest hoặc LightGBM nhỏ (scikit-learn), chạy trên Pi trong vài ms. Dữ liệu ít
nên **không dùng deep learning** cho phần này; cây quyết định cho ra feature importance để giải
thích trên slide ("mô hình dựa vào tỉ lệ PM1/PM2.5 và Δđộ ẩm nhiều nhất").

**Lấy nhãn — điểm "hay" để trình bày (human-in-the-loop):**

1. Thí nghiệm có kiểm soát: mỗi loại 8–10 lần trong hộp khói/phòng nhỏ (nén hương, giấy đốt nhỏ,
   bếp đun nước/xào, rũ khăn bụi, xịt cồn gần MQ135). Ghi giờ bắt đầu – kết thúc. Theo quy định
   an toàn hiện có: lượng nhỏ, nơi thoáng, có người trông.
2. Khi chạy thật, bot hỏi lại qua Telegram: *"Phát hiện PM2.5 tăng nhanh lúc 18:42. Đây là gì?"*
   kèm nút `[Nấu ăn] [Hương] [Thuốc lá] [Bụi] [Khác] [Không biết]`. Câu trả lời ghi vào bảng
   `event_labels` → mô hình học thêm theo thời gian (active learning). Đây là tính năng sản phẩm
   thật: càng dùng càng đúng với nhà mình.

**Đánh giá trung thực:** chia train/test **theo phiên thí nghiệm** (không xáo trộn từng mẫu, vì các
mẫu trong cùng một lần đốt rất giống nhau), báo confusion matrix, so với một luật heuristic đơn giản
làm đối chứng (ví dụ "MQ135 bật mà PM không tăng → hoá chất").

**Rủi ro cần nói trước:** PMS7003 đo kém hạt thô, PM10 phần lớn được suy ra từ các kênh hạt nhỏ,
nên phân biệt "bụi" có thể yếu; DHT11 có sai số lớn (±2 °C, ±5 %RH) nên tín hiệu nấu ăn có thể
mờ. Nếu một lớp không phân biệt được thì gộp lớp và ghi rõ trong báo cáo.

**Ứng dụng trong sản phẩm:** tin Telegram có nguyên nhân ("Có vẻ đang **nấu ăn** (82%) — đã bật
quạt, dự kiến về mức Tốt sau ~20 phút"); báo cáo sáng thống kê "hôm qua 3 lần nấu ăn, 1 lần
hương, nguồn chính gây ô nhiễm là bếp"; luật quạt có thể dùng nhãn (ví dụ hơi hoá chất → khuyên mở
cửa sổ thay vì chỉ bật quạt lọc hạt, vì quạt lọc bụi không lọc khí).

### (B) Hiệu chỉnh PMS7003 theo độ ẩm — nối PMS7003 với DHT11

**Cơ sở:** cảm biến Plantower (PurpleAir dùng PMS5003, cùng họ với PMS7003) đọc cao hơn trạm chuẩn,
nhất là khi ẩm. Nghiên cứu của US EPA (Barkjohn và cộng sự, AMT 2021, ~12 000 mẫu 24 giờ ở 16 bang)
cho thấy hồi quy tuyến tính **có thêm hạng độ ẩm** làm giảm sai số rõ rệt (RMSE từ 8 xuống 3 µg/m³),
và mô hình phức tạp hơn không cải thiện đáng kể trên tập độc lập. Hà Nội ẩm, nên điều này đặc biệt
liên quan.

**Làm gì:**

1. Mặc định dùng công thức kiểu EPA: `PM_hiệu_chỉnh = a·PM_thô + b·RH + c`.
2. Khi đã đo ≥ 7 ngày song song với một trạm công khai (đã có trong kế hoạch nghiệm thu), tự fit
   lại `a, b, c` và thử thêm Random Forest/hồi quy có tương tác `PM×RH`; báo cáo MAE trước/sau.
3. Dùng PM đã hiệu chỉnh cho dự báo và nhãn AQI; vẫn lưu số thô.

Ít công, có trích dẫn khoa học, cho ra một biểu đồ "trước/sau hiệu chỉnh" rất dễ thuyết phục.
`scripts/acceptance_report.py` đã tính `công khai ≈ a·trạm + b`; chỉ cần mở rộng thêm độ ẩm.

### (C) Mô hình phòng: thời gian về sạch, hiệu quả quạt, mở cửa sổ hay không

**Làm gì:** học từ chính dữ liệu của trạm một mô hình vật lý đơn giản (physics-informed):

`dC/dt = S(t) − (k_tự_nhiên + k_quạt·quạt_bật)·C + k_trao_đổi·(C_ngoài − C)`

- Mỗi lần PM giảm sau đỉnh, fit đường mũ để ước lượng `k` khi quạt bật và khi tắt. Có đủ vài
  chục đoạn là ra phân bố `k_quạt` → **đo được hiệu quả quạt bằng số** (đúng mục "Hiệu quả lọc"
  trong bảng nghiệm thu, hiện chưa có cách đo tốt).
- Từ `k` hiện tại dự đoán **"còn bao nhiêu phút để về mức Tốt/Trung bình"** — câu trả lời người
  dùng thực sự muốn biết, hữu ích hơn dự báo 6 giờ.
- Kết hợp PM2.5 ngoài trời từ Open-Meteo Air Quality: nếu ngoài sạch hơn trong nhà nhiều → khuyên
  mở cửa sổ; nếu ngoài bẩn (dự báo) → khuyên đóng cửa, bật quạt chủ động.

### (D) Phát hiện bất thường và lỗi cảm biến bằng kiểm tra chéo

**Làm gì:** Isolation Forest (hoặc autoencoder nhỏ) trên vector ngữ cảnh, kết hợp vài kiểm tra chéo
có tính vật lý: PM đứng yên một giá trị quá lâu, PM1 > PM2.5, độ ẩm nhảy 30 % trong 10 giây, PM
tăng vọt mà không có thay đổi nào ở cảm biến khác và không có người.

**Giá trị:** phân biệt *"cảm biến hỏng"* với *"sự kiện thật"* — chính là phép thử "An toàn" trong
bảng nghiệm thu; hiện controller chỉ biết "mất dữ liệu > 2 phút". Đây cũng là minh chứng rõ nhất cho
việc các cảm biến **xác nhận lẫn nhau** thay vì chạy rời.

### (E) Gemini thành trợ lý hội thoại có công cụ chỉ-đọc

**Làm gì:** cho phép nhắn tự do trong Telegram, ví dụ *"Hôm qua lúc nào không khí tệ nhất, vì
sao?"*, *"Tối nay có nên mở cửa sổ không?"*. Gemini dùng **function calling** (tài liệu Gemini API
mô tả đúng trường hợp "truy cập thông tin từ cơ sở dữ liệu") với vài hàm **chỉ đọc**:
`get_summary(start, end)`, `get_events(start, end)`, `get_forecast()`, `get_outdoor_aq()`,
`get_time_to_clean()`.

- Giữ nguyên nguyên tắc an toàn: không có hàm nào bật/tắt quạt. Nếu cần, Gemini chỉ *đề xuất* và
  bot hiện nút để **người dùng** bấm `/fan on`.
- Kết hợp (A): Gemini giải thích sự kiện bằng ngôn ngữ tự nhiên từ đặc trưng và feature importance
  ("PM1 gần bằng PM2.5 và độ ẩm tăng 6 % → giống khói nấu ăn").
- Ràng buộc chi phí: giới hạn số lượt hỏi/ngày trong `config.yaml`, hết lượt thì trả lời bằng mẫu.
- (Tuỳ chọn) ảnh bầu trời từ camera ban công gửi Gemini để mô tả mây/trời mưa, **chỉ khi YOLO
  không thấy người trong ảnh** — vừa thêm chức năng, vừa là một ví dụ fusion cho quyền riêng tư.

Đây là phần "wow" khi trình bày, nhưng phụ thuộc Internet và hạn mức, nên đặt sau A–C.

### (F) Camera: từ "chạy riêng" thành nguồn nhãn

Ý tưởng có tiền lệ: bộ dữ liệu **UCI Occupancy Detection** (Candanedo & Feldheim, Energy and
Buildings 2016) phân loại phòng có người hay không từ nhiệt độ, độ ẩm, ánh sáng, CO₂, với nhãn lấy
từ **ảnh chụp mỗi phút**. Nhóm có đúng bộ phận tương tự: YOLO trên camera tự gán nhãn "có người",
rồi huấn luyện mô hình chỉ dùng cảm biến môi trường (lux, nhiệt, ẩm, PM) để đoán có người. Sau đó có
thể tắt camera mà vẫn biết phòng có người — **quyền riêng tư** là một điểm cộng khi trình bày. Có thể
pre-train trên bộ UCI rồi đối chiếu với dữ liệu nhà mình (lưu ý: mạch không có cảm biến CO₂).

Việc cần làm ngay để camera không còn rời: ghi kết quả YOLO (số người, độ tin cậy) vào SQLite thay
vì chỉ đẩy MQTT, và gộp việc dùng camera vào một tiến trình (tránh tranh `/dev/video0` với
`aiot/weather.py`).

## 4. Thay đổi kiến trúc để hết "rời rạc"

1. **Bảng `features_1m`** (hoặc mở rộng `readings`) — một dòng mỗi phút chứa đủ mọi cảm biến + số
   người + PM ngoài trời + trạng thái quạt. Mọi mô hình đọc từ đây.
2. **Dịch vụ `aiot-insight`** (mới, lặp 60 s): tính đặc trưng, chạy A/C/D, ghi bảng `insights`
   (`ts, kind, label, confidence, detail_json`). Controller, bot, báo cáo sáng và dashboard đều đọc
   bảng này. Controller vẫn quyết định bằng luật, chỉ thêm *lý do* và vài luật dùng nhãn.
3. **Dashboard "Tình trạng nhà"**: một thẻ lớn thay cho các biểu đồ riêng từng cảm biến:
   *"Đang nấu ăn (82%) · PM2.5 68 → về Tốt sau ~18 phút · Quạt: BẬT (tự động) · Cảm biến: bình
   thường"*, kèm thanh xác suất các lớp và 3 đặc trưng ảnh hưởng nhất. Biểu đồ chung một trục thời
   gian, tô màu nền theo sự kiện đã nhận diện.

## 5. Kịch bản demo đề xuất (≈ 5 phút)

1. Dashboard ở trạng thái bình thường; giới thiệu "trạm hiểu ngữ cảnh" bằng sơ đồ mục 2.
2. Thắp một nén hương cạnh hộp cảm biến (có người trông). Sau ~1 phút: dashboard hiện *"Khói
   hương/đốt (xx%)"*, quạt bật, Telegram nhận tin kèm nguyên nhân và thời gian về sạch dự kiến.
3. Xịt cồn gần MQ135 (không có bụi): hệ thống báo *"hơi hoá chất — quạt lọc bụi không xử lý được
   khí, nên mở cửa sổ"* → cho thấy AI phân biệt được, không chỉ "PM cao thì bật quạt".
4. Hỏi bot: *"Vừa rồi chuyện gì xảy ra?"* → Gemini tóm tắt hai sự kiện từ dữ liệu thật.
5. Rút nguồn bằng cách dừng collector (theo quy định không tháo dây khi Pi có điện): hệ thống báo
   *lỗi cảm biến*, không bật/tắt bừa.
6. Slide kết quả: confusion matrix (A), biểu đồ hiệu chỉnh trước/sau (B), `k_quạt` đo được (C),
   và bảng "mỗi cảm biến dùng cho những gì".

Chế độ demo hiện có (rút ngắn thời gian chờ 20 s) dùng lại được cho bước 2–3.

## 6. Lộ trình gợi ý

Mốc nộp bài chưa chốt, nên chia theo mức ưu tiên; mỗi giai đoạn đều tự đứng được nếu hết giờ.

| Giai đoạn             | Việc                                                                                                                                                                         | Kết quả trình bày được                                               |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| 1 (≈ 1 tuần)          | Bảng `features_1m`, ghi YOLO vào DB; hiệu chỉnh độ ẩm kiểu EPA (B); fit `k` suy giảm và "thời gian về sạch" (C); bộ phát hiện sự kiện bằng heuristic + nút gán nhãn Telegram | Thẻ "Tình trạng nhà", tin có thời gian về sạch, bắt đầu thu nhãn     |
| 2 (≈ 1 tuần)          | Thí nghiệm có kiểm soát lấy nhãn; huấn luyện Random Forest (A); Isolation Forest + kiểm tra chéo (D)                                                                         | Demo nhận diện hương/cồn/nấu ăn, confusion matrix, demo lỗi cảm biến |
| 3 (nếu còn thời gian) | Gemini function calling chỉ-đọc (E); mô hình đoán có người (F)                                                      | Hỏi-đáp tiếng Việt, MAE trên dữ liệu thật                            |

Khuyến nghị: **nếu chỉ làm được một thứ, làm (A) cùng bảng `features_1m`** — nó giải quyết cả hai
ý của giảng viên cùng lúc (AI có ứng dụng rõ + cảm biến được kết hợp), và là phần demo trực tiếp
mạnh nhất. (B) và (C) rẻ, nên làm kèm để có số liệu khoa học.

## 7. Những điều cần nói thật trong báo cáo

- Dữ liệu nhãn ít (vài chục phiên) → mô hình nhỏ, đánh giá theo phiên, có khoảng tin cậy; không
  tuyên bố độ chính xác chung cho mọi nhà.
- MQ135 chỉ có bit vượt ngưỡng, ngưỡng chưa thử với khí thật; PMS7003 yếu với hạt thô; DHT11 kém
  chính xác. Đây là giới hạn phần cứng, ghi vào phần hạn chế.
- Quạt mini là mô phỏng (proof of concept); `k_quạt` đo được chỉ đúng cho hộp/phòng thử.
- Camera: chỉ lưu số người, không lưu ảnh có người; nêu rõ chính sách này khi trình bày.

## Nguồn đã đối chiếu

- Barkjohn, Gantt, Clements (2021), *Development and application of a United States-wide correction
  for PM2.5 data collected with the PurpleAir sensor*, Atmospheric Measurement Techniques 14, 4617–4637.
  https://amt.copernicus.org/articles/14/4617/2021/
- Candanedo, Feldheim (2016), *Accurate occupancy detection of an office room from light,
  temperature, humidity and CO2 measurements using statistical learning models*, Energy and Buildings
  112; bộ dữ liệu: https://archive.ics.uci.edu/dataset/357/occupancy+detection
- Gemini API — Function calling: https://ai.google.dev/gemini-api/docs/function-calling

Các nhận định về tỉ lệ kích thước hạt theo nguồn (khói đốt rất mịn, bụi thô) và hạn chế của PMS7003
với hạt thô là kiến thức chung trong tài liệu về cảm biến giá rẻ, chưa được đối chiếu với một nguồn
cụ thể trong lần research này (công cụ tìm kiếm web bị lỗi); nên tìm thêm 1–2 bài báo trước khi đưa
vào báo cáo chính thức.
