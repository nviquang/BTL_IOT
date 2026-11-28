# Khép vòng trạm AIoT: servo, ThingsBoard, mô hình phòng

Hướng đi hợp lý nhất gồm bốn việc. Một là bỏ hẳn MQ135 khỏi mạch, khỏi code và khỏi luật điều khiển. Hai là thêm **một servo SG90 đóng/mở cửa gió của hộp mô hình phòng**. Ba là đẩy dữ liệu lên **ThingsBoard Cloud Free**, nơi giữ dữ liệu, hiển thị dashboard và gửi lệnh ngược về Pi. Bốn là thay "AI dự báo giờ" bằng một **mô hình phòng theo vật lý cộng bộ nhận diện sự kiện nhỏ (RandomForest)**, huấn luyện trên dữ liệu sinh từ chính các phiên đo thật. Bốn việc này nối thành một vòng kín: PMS7003 thấy khói, AI cho biết đó là loại sự kiện gì và bao lâu thì sạch, luật cục bộ bật quạt và quyết định đóng hay mở cửa gió, còn dashboard trên cloud cho xem và cho bấm ghi đè. Hệ thống hiện tại tương đương **IoT cấp 1** theo slide môn học, vì mọi thứ nằm trong mạng LAN. Làm xong phương án này thì đúng **cấp 2**: một nút, phân tích tại chỗ, dữ liệu và ứng dụng trên đám mây. Muốn lên cấp 4 thì phải có nút thứ hai có phân tích cục bộ, ví dụ một ESP32. Việc đó làm được nhưng tốn thêm thời gian. Phần AI cần được kể trung thực. Nhóm chỉ có vài chục phiên thật, dữ liệu sinh thêm chỉ nội suy những gì đã đo, và kết quả phải chia theo phiên, có bảng so với luật ngưỡng viết tay. Tổng công sức ước khoảng **12–16 ngày-người**, chia được cho 3–4 người trong 1,5–2 tuần.

## Bỏ MQ135 là việc dọn dẹp khoảng 30 file, không phải rút một sợi dây

Lý do bỏ thì rõ. MQ135 chỉ nối chân DO nên chỉ có một bit "vượt ngưỡng", không có nồng độ. Ngưỡng do biến trở đặt và chưa thử với khí thật. Khi mất GND, cảm biến vẫn đọc `1`. Nhưng bit này đã nằm trong khá nhiều code: `aiot/sensors.py`, `collector.py`, `aggregate.py`, cột `mq135_do` trong `db.py`, nhánh "báo khí" trong `control.py` và `controller.py` (nhánh này ép quạt bật và gửi Telegram cả trong giờ yên lặng), cùng `report.py`, `notifier.py`, `payload.py`, `healthcheck.py`, lệnh `/now` trong `bot.py`, `advice.py`, `server_app.py`, `test_all.py`, nhiều test, và các tài liệu `CLAUDE.md`, `README.md`, `so_do_noi_day_raspberry_pi.md`, `docs/van_hanh_chi_tiet.md`.

Cách làm an toàn là xoá cảm biến và toàn bộ nhánh điều khiển "báo khí", nhưng **giữ cột `mq135_do` dạng NULL được** để dữ liệu cũ và các truy vấn cũ không gãy. Nếu muốn xoá cột thì viết migration riêng. Tháo MQ135 sẽ giải phóng **GPIO26 (pin 37)**, chân 5V pin 4 và GND pin 20. GPIO26 có GND ở pin 39 ngay cạnh, rất tiện cho dây tín hiệu servo. Có một hệ quả cho phần AI: bộ nhận diện sự kiện mất đặc trưng khí, nên lớp "hơi hoá chất" trong các bản nháp `docs/de_xuat_cai_thien_ai*.md` phải bỏ. Nhớ tắt Pi và rút nguồn trước khi tháo dây.

## Servo cửa gió là cơ cấu duy nhất đóng được vòng vật lý với cả hai nhánh AI

Ghi chú nghiên cứu xếp năm phương án. Servo SG90/MG90S kéo cửa gió hoặc mái che đứng đầu. Sau đó là còi + LED RGB, kênh relay thứ hai, động cơ bước 28BYJ-48, và máy phun sương. Servo thắng vì ba lý do. Nó có chuyển động thấy rõ. Nó ăn khớp với cả nhánh chất lượng không khí lẫn nhánh thời tiết (`weather.py`). Và nó rẻ: SG90 khoảng **42.000 ₫**, MG90S bánh răng kim loại khoảng **85.000 ₫** ([Hshop](https://hshop.vn/search?q=servo+sg90)). Theo định nghĩa trong giáo trình, cơ cấu chấp hành là bộ chuyển đổi biến tín hiệu lệnh thành **tác động vật lý lên quá trình** ([Hands-On Industrial IoT](https://www.oreilly.com/library/view/hands-on-industrial-internet/9781789537222/52d82ea2-6cd7-44f0-81c0-b5ebaf8e932f.xhtml)). IEEE còn nhấn mạnh yếu tố tạo chuyển động ([IEEE BLP](https://blp.ieee.org/what-is-actuator-in-iot/)). Servo và quạt đều đúng nghĩa hẹp này. Còi và LED chỉ báo hiệu, không tác động lên PM2.5. Nếu thêm chúng thì gọi là "cơ cấu báo hiệu" để tránh bị bắt bẻ. Phun sương thì không nên làm: đưa nước lại gần mạch, hiếm khi kích hoạt vì Hà Nội vốn ẩm, và hơi ẩm làm PMS7003 đọc cao.

Nhóm từng bỏ mô hình cửa servo vì phức tạp. Lần này nên giữ đơn giản: **hai vị trí (mở 0°, đóng 90°), không công tắc hành trình, không lỗi "cửa kẹt", không lệnh `/door`**. Chỉ thêm một lệnh ghi đè `/vent open|close` đi qua cùng cơ chế ghi đè thủ công của quạt.

Về điện, servo phải lấy nguồn riêng. GPIO của Pi chỉ chịu 3,3 V, tối đa 16 mA mỗi chân và 50 mA tổng. Thiết bị nối thẳng vào chân 5V thì đi vòng qua PMIC nên Pi không giám sát được dòng ([Raspberry Pi documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html)). SG90 kéo khoảng 100–250 mA khi quay, và **dòng kẹt có thể lên 360–650 mA** ([ProtoSupplies](https://protosupplies.com/product/servo-motor-micro-sg90/); [Circuit Rocks](https://docs.circuit.rocks/motors/sg90-servo/)). Rail 5V của Pi lại đang nuôi quạt, PMS7003 và camera. Vì vậy cấp servo bằng một sạc USB 5V/2A rời, thêm tụ ≥ 100 µF giữa 5V và GND của servo, nối GND chung với Pi (pin 39). Dây tín hiệu đi thẳng từ GPIO26.

Về phần mềm, gpiozero từ bản 2.0 dùng lgpio làm mặc định ([gpiozero changelog](https://gpiozero.readthedocs.io/en/latest/changelog.html)). Nhưng xung servo của lgpio được tạo bằng phần mềm, và chính tác giả cảnh báo servo sẽ "fidget" ([lgpio docs](https://abyz.me.uk/lg/py_lgpio.html)). Với cơ cấu hai vị trí, cách tránh rung đơn giản nhất là phát xung khoảng 1 giây rồi đặt `servo.value = None` để ngừng xung ([gpiozero Output API](https://gpiozero.readthedocs.io/en/stable/api_output.html)). Chỉ khi cách này không đủ mới chuyển sang PWM phần cứng trên GPIO12 hoặc `pigpiod`. Cấu hình overlay cho Bookworm/Trixie chưa được kiểm chứng.

Ghi chú về lọc: quạt 5V không có màng lọc chủ yếu chỉ khuấy không khí. Để thấy rõ hiệu quả quạt, nên **gắn quạt vào một miếng lọc HEPA/MERV rẻ** (kiểu Corsi–Rosenthal mini). Đây là suy luận, vì chưa có số CADR nào được công bố cho quạt mini. Nhóm phải tự đo.

## ThingsBoard Cloud Free lo cả kho dữ liệu, dashboard và lệnh ngược

Để chọn dịch vụ online, ghi chú so sánh 13 ứng viên. ThingsBoard Cloud Free nổi bật vì một dịch vụ làm đủ ba vai: lưu trữ, dashboard và lệnh xuống thiết bị. Gói này cho **5 thiết bị, 1 triệu data point mỗi tháng, không cần thẻ, không hết hạn** ([ThingsBoard pricing](https://thingsboard.io/pricing/)). Dữ liệu time-series giữ 30 ngày. Giới hạn là 0,5 triệu message mỗi tháng và 600 message mỗi phút cho mỗi thiết bị ([ThingsBoard subscriptions](https://thingsboard.io/docs/paas/reference/subscriptions/)). Pi chỉ cần paho-mqtt (đã có trong repo) với TLS cổng 8883, dùng access token làm username. Pi publish lên `v1/devices/me/telemetry`. Để nhận lệnh, Pi subscribe `v1/devices/me/rpc/request/+` hoặc `v1/devices/me/attributes` ([ThingsBoard MQTT API](https://thingsboard.io/docs/reference/mqtt-api/)). Kết nối do Pi chủ động mở ra ngoài nên đi qua NAT được, không cần mở port.

Phải tính lưu lượng. Đẩy mẫu 10 giây với khoảng 8 trường thì ra khoảng 2,07 triệu data point mỗi tháng, vượt hạn mức. Đẩy **trung bình 1 phút** với khoảng 10 trường (PM1, PM2.5, PM10, nhiệt độ, độ ẩm, lux, quạt, cửa gió, nhãn sự kiện, thời gian còn lại đến khi sạch) thì ra khoảng 430 nghìn, vẫn dưới 1 triệu. ThingsBoard nhận mảng `{ts, values}` gắn timestamp từ thiết bị ([ThingsBoard telemetry](https://thingsboard.io/docs/user-guide/telemetry/)). Nhờ vậy có thể làm một tiến trình "uplink" giống `outbox` của Telegram. Tiến trình này đọc SQLite theo một con trỏ `ts` lưu trong bảng `state`, gửi bằng QoS 1, và chỉ tiến con trỏ khi nhận PUBACK. Khi mất mạng, nó gửi bù bản ghi 5 phút theo lô. Nguyên tắc local-first được giữ: SQLite trên Pi vẫn là nguồn sự thật.

**Lệnh từ dashboard không được ghi thẳng vào GPIO.** Nút bật/tắt quạt và đóng/mở cửa gió phải đi vào đúng đường ghi đè thủ công đang có (hết hạn sau 2 giờ). Khi đó luật cục bộ vẫn quyết định cuối cùng, kể cả lúc mất Internet.

Phương án dự phòng là Streamlit Community Cloud. Nhưng không thể đẩy nguyên `server_app.py` lên. App ngủ sau **12 giờ không có người xem** và không có gói trả phí giữ thức ([Streamlit docs](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)). Filesystem là tạm thời. `BROKER = "localhost"` và đường dẫn `C:\mqtt\data` sẽ hỏng. Luồng nền của paho không được gọi hàm Streamlit ([Streamlit multithreading](https://docs.streamlit.io/develop/concepts/design/multithreading)). Muốn đi đường này thì phải ghép thêm broker cloud (EMQX/HiveMQ) và một kho dữ liệu, rồi tự viết phần đăng nhập cho nút điều khiển. Nếu chỉ cần "có dashboard online" cho đúng bài trong slide thì ThingSpeak dễ nhất. Đổi lại, nó bị chặn 15 giây mỗi lần cập nhật, và MQTT subscribe chỉ có ở license trả phí ([ThingSpeak license](https://thingspeak.mathworks.com/prices/thingspeak_standard)). Nên giữ Streamlit cục bộ cho phần phân tích sâu và camera nếu muốn.

## Mô hình phòng và bộ nhận diện sự kiện


Phần AI chính nên là **mô hình phòng theo cân bằng khối lượng**. Nguồn phát làm PM tăng. Khi nguồn dừng, PM giảm theo hàm mũ với tốc độ k = trao đổi khí + lắng đọng + CADR/V ([LBL compilation](https://indoor.lbl.gov/sites/default/files/brett_singer_-_compilation_of_published_pm_2.5_emission_rates_for_cooking_candles_and_incense_for_the_use_in_modeling_of_exposeres_in_residences.pdf)). Trong một nhà thật, 836 sự kiện cho tốc độ suy giảm trung vị khoảng **1,3 h⁻¹** và trung bình khoảng **2,0 h⁻¹** ([ASHRAE 1756-RP](https://www.researchgate.net/publication/336249268_Response_of_eight_low-cost_particle_sensors_and_consumer_devices_to_typical_indoor_emission_events_in_a_real_home_ASHRAE_1756-RP)). Nghĩa là một sự kiện tắt dần trong 0,5–2 giờ ở phòng thật. Trong hộp 50–200 lít thì chỉ mất vài phút, nên một ngày thu được hơn 10 phiên.

Từ mỗi phiên, fit được k khi quạt tắt (k_off) và khi quạt bật (k_on). Hiệu k_on − k_off chính là tác dụng của quạt. Từ đó tính được thời gian đến khi sạch theo công thức t = ln((C0 − C_bg)/(C* − C_bg))/k. Đây là phần "fine-tune" tiết kiệm dữ liệu nhất, vì chỉ fit 2–4 tham số. Nó cũng cho một câu nói đo được và trung thực: "quạt tăng tốc độ làm sạch thêm X h⁻¹, thời gian sạch giảm từ A xuống B phút".

Mô hình phòng còn làm **bộ sinh dữ liệu** cho bộ nhận diện sự kiện. Đây là một RandomForest/ExtraTrees nhỏ (100–300 cây, độ sâu 6–10) dùng 10–20 đặc trưng tự thiết kế: mức PM trên nền, các tỉ lệ PM1/PM2.5 và PM2.5/PM10, độ dốc khi tăng, đỉnh, thời lượng, k sau đỉnh, ΔRH, lux, trạng thái quạt. Tài liệu cho thấy nguồn trong nhà đóng góp nhiều hơn ở hạt to, và hạt to tắt nhanh hơn hạt mịn ([Rathbone et al. 2025](https://pmc.ncbi.nlm.nih.gov/articles/PMC11729851/)). Vì vậy tỉ lệ kích thước hạt tách được **"khói đốt, hạt mịn"** (nhang, giấy) khỏi **"bụi thô, tung bụi"** (rũ khăn, quét). Còn tách nhang khỏi nấu ăn trên PMS7003 thì dễ thất bại. Nhóm nên sẵn sàng gộp chúng thành một lớp. Cảm biến giá rẻ còn bỏ sót gần hết hạt dưới 0,3 µm ([Singer & Delp 2018](https://indoor.lbl.gov/publications/response-consumer-and-research-grade)). Nghiên cứu nhiều tuần trên 3 nhà cũng không xác định được nguồn ngoài mức "trong nhà hay ngoài trời" khi chỉ dùng dữ liệu thụ động ([Rathbone et al. 2025](https://pmc.ncbi.nlm.nih.gov/articles/PMC11729851/)). Đó là lý do phải **dàn dựng và gán nhãn từng phiên**. Con số 98% của một nghiên cứu PurpleAir năm 2026 dùng thêm cảm biến VOC và không kiểm tra được cách chia dữ liệu ([ScienceDirect](https://www.sciencedirect.com/science/article/pii/S2666827026000678)). Không nên lấy con số đó làm kỳ vọng.

Chọn cây quyết định thay vì mạng sâu vì dữ liệu ít và cần giải thích được. Một CNN-1D trên Raspberry Pi mất khoảng 20 ms mỗi lần suy luận ([Sensors 23:6227](https://pmc.ncbi.nlm.nih.gov/articles/PMC10347208/table/sensors-23-06227-t008)), tức chạy được. Nhưng nó cần dữ liệu sinh mới học nổi, và khó giải thích khi demo. Rừng cây cho câu trả lời kiểu "là khói vì PM1/PM2.5 = 0,9 và PM tăng 40 µg/m³/phút". Biểu đồ feature importance làm được một slide. Đây chính là thứ giảng viên cần sau nhận xét "AI chưa có tính ứng dụng, các cảm biến rời rạc". Chronos-Bolt Tiny (9M tham số) cần PyTorch, nặng với Pi 4, nên nếu dùng thì chỉ làm baseline chạy offline trên laptop ([Hugging Face](https://huggingface.co/autogluon/chronos-bolt-tiny)).

### Kế hoạch dữ liệu: ít phiên thật, sinh thêm có kiểm soát, đánh giá theo phiên

| Hạng mục | Kế hoạch |
|---|---|
| Phiên thật | 2–3 ngày đo. Mỗi ngày 3–4 phiên khói đốt (nhang hoặc giấy, lượng nhỏ, nơi thoáng, có người trông), 2–3 phiên bụi thô. Một nửa phiên bật quạt, một nửa tắt. Xen kẽ các khối "bình thường" 15–30 phút ở cùng khung giờ. Mục tiêu ≥ 5–8 phiên mỗi lớp. **Giữ nguyên một ngày làm tập test cuối, không đụng tới.** |
| Sinh dữ liệu | Fit C_bg, thời lượng nguồn τ, cường độ S/V, k_off, k_on, tỉ lệ kích thước hạt cho từng lớp. Lấy mẫu tham số trong khoảng đã fit, nới ±20–30%. Giải ODE bước 10 s. Thêm lớp nhiễu cảm biến: độ lệch chuẩn ước từ đoạn nền, lượng tử hoá số nguyên, mất mẫu, lượng tử 1 °C/1 %RH của DHT11, sai lệch khi RH > 75%. Thêm biến đổi nhẹ: nhiễu, cắt cửa sổ, co giãn biên độ, window warping. **Không** đảo chiều, hoán vị hay lật, vì những phép đó phá dạng "tăng rồi giảm" ([Iwana & Uchida 2021](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0254841)). Sinh 50–200 phiên mỗi lớp **trong từng fold**. |
| Chia dữ liệu | Leave-One-Session-Out hoặc StratifiedGroupKFold theo phiên. Không bao giờ chia theo cửa sổ ([scikit-learn](https://scikit-learn.org/stable/modules/cross_validation.html)). Bộ sinh và scaler phải fit lại bên trong mỗi fold. |
| Bảng kết quả | Các dòng: luật ngưỡng viết tay / RF chỉ dữ liệu thật / RF thật + biến đổi / RF chỉ dữ liệu sinh (TSTR) / RF thật + sinh. Báo macro-F1 ± SD qua các fold, ma trận nhầm lẫn cộng dồn, kết quả trên ngày test cuối ([Esteban et al. 2017](https://arxiv.org/pdf/1706.02633)). Với nowcast +10/+20/+30 phút: MAE so với persistence và với "vật lý dùng k mặc định", cộng sai số thời gian đến khi sạch tính bằng phút. |

Có một số bẫy rò rỉ cần tránh. Cửa sổ chồng lấn của cùng một phiên nằm cả ở train lẫn test là bẫy lớn nhất. Fit bộ sinh trên mọi phiên, kể cả phiên test, cũng là rò rỉ. Một bẫy nữa là khối "bình thường" luôn được thu ở giờ khác, khi đó mô hình học giờ hoặc lux thay vì học sự kiện.

Độ ẩm là biến gây nhiễu thật. Họ cảm biến Plantower đọc tăng rõ khi RH vượt khoảng 75%: trong buồng thử, PM2.5 đi từ khoảng 9 lên 16 µg/m³ khi RH tăng từ 78% lên 89% ([Jayaratne et al. 2018](https://amt.copernicus.org/articles/11/4883/2018/)). Công thức hiệu chỉnh của EPA được fit trên dữ liệu PurpleAir trung bình 24 giờ ([Barkjohn et al. 2021](https://amt.copernicus.org/articles/14/4617/2021/)). Vì vậy chỉ ghi nó là "hiệu chỉnh kiểu EPA, chưa kiểm chứng tại chỗ", và gắn cờ các cửa sổ ẩm cao.

**Nếu kết quả yếu**, nói thẳng điều đó trong báo cáo. Có thể viết: bộ sinh chỉ nội suy những dạng đã đo, không tạo ra loại nguồn chưa từng thu. Hoặc: RF không hơn luật ngưỡng với n phiên. Hoặc: nhang và nấu ăn không tách được nên đã gộp lớp. Phần vật lý (k_off, k_on, thời gian đến khi sạch) vẫn là kết quả bảo vệ được dù bộ phân loại kém.

## Một vòng kín từ cảm biến đến cloud, quyết định cuối vẫn ở Pi

Cách ghép ba phần dưới đây là đề xuất thiết kế của báo cáo này, chưa có tài liệu nào kiểm chứng. Bộ nhận diện sự kiện quyết định *loại* hành động cho cửa gió. Nếu PM tăng nhanh, hạt mịn, có dạng nguồn trong nhà, và Open-Meteo cho biết ngoài trời tốt, thì **mở cửa gió và bật quạt** để thải khói ra. Nếu PM tăng chậm, không có dạng sự kiện, và dự báo ngoài trời xấu (hoặc `weather.py` báo mưa), thì **đóng cửa gió và bật quạt lọc tuần hoàn**. Khi AI không chắc (xác suất thấp, thiếu dữ liệu Open-Meteo), luật mặc định giữ nguyên vị trí cửa. Riêng quạt vẫn bật/tắt theo ngưỡng PM2.5 có trễ như hiện nay. Gemini chỉ viết lời khuyên. Lệnh từ dashboard và Telegram đều đi qua đường ghi đè.

```
            Internet (chỉ để xem, ghi đè, lời khuyên)
  ┌──────────────────────────────────────────────────────────┐
  │ ThingsBoard Cloud  ── dashboard: PM, quạt, cửa gió,      │
  │   (lưu 30 ngày)       nhãn sự kiện, thời gian đến sạch   │
  │        ▲ telemetry 1 phút        │ RPC: fan/vent on|off  │
  │ Telegram ◄── outbox     Gemini ◄─ JSON tổng hợp (advice) │
  │ Open-Meteo ──► dự báo ngoài trời, mưa                    │
  └────────┼─────────────────────────┼───────────────────────┘
           │ MQTT TLS 8883 (Pi chủ động mở, qua NAT)
  ┌────────┼─────────────────────────▼───────────────────────┐
  │ Raspberry Pi 4  (local-first, chạy cả khi mất mạng)      │
  │  collector 10 s ─► SQLite (raw, readings 5', events)     │
  │      │                 └─► uplink (con trỏ ts, gửi bù)   │
  │      ▼                                                   │
  │  AI cục bộ: mô hình phòng (k_off, k_on, nowcast,         │
  │             thời gian đến sạch) + RF nhận diện sự kiện   │
  │      ▼                                                   │
  │  controller (luật tất định, ghi đè thủ công 2 giờ)       │
  │      ├─► GPIO23 relay ─► quạt + màng lọc                 │
  │      └─► GPIO26 servo (nguồn 5V riêng) ─► cửa gió hộp    │
  │  PMS7003, DHT11, BH1750, camera USB (ảnh bằng chứng)     │
  └──────────────────────────────────────────────────────────┘
```

**Kịch bản demo khoảng 2–3 phút.** Bật chế độ demo 20 giây và mở dashboard ThingsBoard trên máy chiếu. Đốt một que nhang nhỏ cạnh lỗ hút của hộp. Trong khoảng 30–60 giây, PM2.5 vọt lên và dashboard đổi nhãn thành "khói đốt". Ngoài trời đang tốt nên servo mở cửa gió, quạt bật, Telegram nhận tin kèm ảnh camera, và dashboard hiện "dự kiến sạch sau N phút". Rút nhang ra để mọi người thấy đường PM giảm theo hàm mũ và so với đường quạt tắt đã ghi từ trước. Tiếp theo, rũ một miếng khăn bụi để có nhãn "bụi thô". Che BH1750 và giả lập cờ mưa (hoặc dùng ngưỡng demo của `weather.py`) để cửa gió đóng. Cuối cùng bấm nút "tắt quạt" trên dashboard để thấy lệnh đi qua ghi đè và được ghi vào nhật ký. Sau đó rút cáp mạng để cho thấy luật cục bộ vẫn chạy, rồi cắm lại để dữ liệu được gửi bù.

## Theo thang của slide, hệ thống đang ở cấp 1 và sẽ lên cấp 2

Slide môn học (Chương 1, mục 1.7, theo Bahga & Madisetti) mô tả các cấp như sau. **Cấp 1**: một nút, lưu trữ, phân tích và ứng dụng đều cục bộ. **Cấp 2**: một nút, phân tích cục bộ, dữ liệu lưu trên đám mây, ứng dụng thường dựa trên đám mây. **Cấp 3**: dữ liệu được lưu *và phân tích* trên đám mây. **Cấp 4**: nhiều nút có phân tích cục bộ, cùng đẩy lên đám mây. **Cấp 5 và 6**: có nút điều phối hoặc bộ điều khiển tập trung trên đám mây.

Hiện tại hệ thống có SQLite trên Pi, MQTT nội bộ và Streamlit trên laptop trong cùng LAN. Đó là cấp 1. Laptop chỉ là máy hiển thị trong mạng cục bộ, không phải đám mây. Sau khi hoàn thiện, hệ thống vẫn là một Pi duy nhất, phân tích + AI + điều khiển chạy tại chỗ, dữ liệu và dashboard trên ThingsBoard, lệnh đi ngược về Pi. Mô tả này khớp đúng cấp 2.

Có thể có người hỏi vặn: Gemini là phân tích trên đám mây, vậy có phải cấp 3 không? Câu trả lời: không. Gemini chỉ viết văn bản từ số liệu đã tổng hợp. Phân tích chính (mô hình phòng, nhận diện sự kiện, dự báo, luật điều khiển) đều ở Pi. Nhóm không nên tự nhận cấp 3, vì đẩy phân tích lên cloud trái với nguyên tắc local-first.

Muốn lên **cấp 4** thì cần nút thứ hai có phân tích cục bộ. Ví dụ một ESP32 + DHT11 hoặc một cảm biến PM thứ hai đặt ngoài ban công, gửi thẳng lên ThingsBoard. ThingsBoard Free cho 5 thiết bị nên vẫn đủ. Đặt servo trên một ESP32 riêng cũng là một cách tạo nút thứ hai. Nhưng việc này thêm firmware, thêm một đường lệnh, và lặp lại rủi ro từng khiến nhóm bỏ mô hình cửa. Chỉ nên làm sau khi vòng cấp 2 đã chạy ổn.

## Hướng làm tiếp theo

| # | Việc | Công sức ước | Xong khi |
|---|---|---|---|
| 0 | Chốt các quyết định ở bảng dưới; mua SG90/MG90S, sạc 5V/2A, tụ 100 µF, miếng lọc; chuẩn bị hộp 50–200 L | 0,5 ngày + chờ hàng | Có đủ linh kiện, phân công rõ |
| 1 | Bỏ MQ135: tháo dây (tắt nguồn trước), xoá nhánh báo khí, giữ cột NULL được, sửa test và tài liệu | 1–1,5 ngày | `pytest` xanh; `test_all.py` không còn MQ135; sơ đồ nối dây và `CLAUDE.md` đã cập nhật |
| 2 | Uplink ThingsBoard: device + token trong `.env`, tiến trình gửi trung bình 1 phút qua TLS kèm con trỏ `ts` và gửi bù; dashboard; RPC đi vào ghi đè | 2 ngày | Dashboard online cập nhật mỗi phút; rút mạng 10 phút rồi cắm lại thì không mất điểm nào; nút dashboard ghi sự kiện `override` |
| 3 | Servo cửa gió: nối GPIO26 với nguồn riêng, `/vent open\|close`, luật đóng/mở trong `controller`, chế độ "phát xung rồi thả" | 1,5–2 ngày | 20 lần đóng/mở liên tiếp không rung, không làm `throttled` khác `0x0`; test đơn vị cho luật cửa gió |
| 4 | Hộp mô hình phòng: lắp PMS7003 trong hộp, quạt + màng lọc, cửa gió | 0,5–1 ngày | Một phiên nhang thử cho thấy đường tăng và giảm rõ trong vài phút |
| 5 | Thu phiên thật theo kế hoạch dữ liệu, ghi nhãn vào bảng `events` | 2–3 ngày | ≥ 5–8 phiên mỗi lớp, có cả quạt bật/tắt, có một ngày test bị khoá |
| 6 | Mô hình phòng + bộ sinh + RF + đánh giá LOSO/TSTR (trên laptop) | 2–3 ngày | Có bảng 5 dòng, ma trận nhầm lẫn, k_off/k_on có khoảng tin cậy, kết quả ngày test |
| 7 | Triển khai lên Pi: RF dạng joblib, nowcast, đẩy nhãn sự kiện và thời gian đến sạch lên dashboard và Telegram; đo độ trễ bằng `timeit` | 1 ngày | Nhãn hiện trên dashboard trong vòng < 1 phút sau sự kiện; dịch vụ chạy qua khởi động lại |
| 8 | Tập demo 3 lần, viết báo cáo (cấp 2, giới hạn, quạt mini là proof of concept) | 1–2 ngày | Demo chạy hết kịch bản trong ≤ 3 phút ba lần liên tiếp |

Bước 2, 3 và 4 làm song song được. Bước 5 cần bước 3 và 4 xong.

| Quyết định nhóm cần chốt | Gợi ý |
|---|---|
| Nền tảng cloud | ThingsBoard Cloud Free (vùng EU hoặc Bắc Mỹ); dự phòng ThingSpeak nếu tài khoản có vấn đề |
| Cửa gió trên hộp hay mái che ban công | Cửa gió trên hộp, vì đóng được vòng với PM; mái che để sau |
| Các lớp sự kiện | Bình thường / khói đốt / bụi thô; gộp nhang và nấu ăn nếu không tách được |
| Chân servo | GPIO26 (software PWM, tiện dây), hoặc GPIO12 nếu muốn để ngỏ PWM phần cứng |
| Có làm nút ESP32 để lên cấp 4 không | Chỉ làm sau bước 7, nếu còn ≥ 3 ngày |
| Xoá hay giữ cột `mq135_do` | Giữ, cho NULL |
| Ai đứng cạnh hộp khi thử khói | Luôn có người trông, lượng khói nhỏ, nơi thoáng |

**Các điểm chưa kiểm chứng** cần nhóm tự kiểm tra trước khi chốt:

- Giới hạn ThingsBoard Free có thể đổi, và chưa rõ tài khoản không hoạt động có bị xoá không.
- Chưa trích được tài liệu về dashboard công khai và quyền bấm nút của customer user trên ThingsBoard.
- Gói HiveMQ Serverless Free không còn trên trang giá.
- Chưa rõ Streamlit Cloud có chặn cổng 8883 hay 5432 không.
- Chưa có số đo jitter của lgpio, chưa kiểm chứng cú pháp overlay PWM trên Bookworm/Trixie.
- Chưa có dòng tối đa chính thức của chân 5V trên header.
- Chưa có CADR của quạt mini.
- Chưa có thời gian khởi động (warm-up) của PMS7003 từ datasheet.
- Chưa đo độ trễ RF trên Pi.
- Chưa có nghiên cứu nào cho thấy dữ liệu sinh bằng vật lý giúp phân loại PM trong nhà ngoài đời thật. Bảng TSTR của chính nhóm sẽ là bằng chứng.
- Giá relay, còi, động cơ bước trong nước chưa tra.

## Kết luận

Điểm then chốt không nằm ở chọn thêm thiết bị gì, mà ở việc **mỗi thành phần phải đi vào cùng một câu chuyện đo được**. Quạt và cửa gió tác động lên đúng đại lượng PMS7003 đo. Mô hình phòng biến tác động đó thành một con số (k_on − k_off, số phút đến khi sạch). Bộ nhận diện sự kiện quyết định nên thải khói ra hay đóng kín để lọc. Dashboard cloud chỉ là cửa sổ nhìn vào và tay ghi đè. Cách kể này trả lời trực tiếp nhận xét "cảm biến rời rạc, AI chưa ứng dụng".

Nó cũng đổi cách nhìn về dữ liệu ít: đó không còn là điểm yếu phải che, vì AI dựa trên vật lý vốn chỉ cần vài tham số. Dữ liệu sinh được dùng có kiểm soát và được đo lợi ích bằng TSTR. Một báo cáo thừa nhận "RF chỉ ngang luật ngưỡng, nhưng quạt có màng lọc rút ngắn thời gian sạch X%" sẽ thuyết phục hơn một con số độ chính xác cao mà không chia theo phiên.
