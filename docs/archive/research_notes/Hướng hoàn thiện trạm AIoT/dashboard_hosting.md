# Hosting dashboard + cloud data store miễn phí cho trạm AIoT (trạng thái tháng 11/2026)

Ghi chú nghiên cứu, đọc nguồn ngày 16/11/2026. Mọi số liệu dưới đây lấy từ trang chính thức nếu có ghi "(chính thức)"; còn lại là từ trang tổng hợp/diễn đàn và cần kiểm tra lại trước khi chốt. Giới hạn free tier có thể đổi bất cứ lúc nào.

Bối cảnh hệ thống (từ repo): Pi đọc cảm biến mỗi 10 s, lưu SQLite, gộp 5 phút, điều khiển quạt cục bộ. Dashboard hiện tại `server_app.py` (Streamlit) kết nối broker `localhost`, `loop_start()` paho-mqtt trong luồng nền, ghi CSV vào `C:\mqtt\data` — tức là phụ thuộc vào máy laptop chạy Mosquitto.

---

## 1. Giới hạn free tier hiện hành của từng ứng viên (còn nhận đăng ký năm 2026 không?)

### Takeaway
ThingsBoard Cloud Free là ứng viên "nền tảng IoT trọn gói" mạnh nhất: 5 thiết bị, 1M data point/tháng, giữ dữ liệu 30 ngày, không cần thẻ, không hết hạn, có sẵn kênh lệnh xuống (RPC / shared attributes). Các dịch vụ "chỉ broker" (HiveMQ, EMQX) và "chỉ host app" (Streamlit, HF Spaces, Render) đều phải ghép thêm một nơi lưu dữ liệu.

### Cited Findings

**ThingsBoard Cloud (PaaS)**
- Free plan: 5 devices, 5 assets, 1M data points/tháng, $0; "No credit card is required, and the plan never expires" (chính thức). Hai vùng: thingsboard.cloud (Bắc Mỹ) và eu.thingsboard.cloud. Gói trả phí rẻ nhất trên cloud là Prototype $49/tháng — [ThingsBoard pricing](https://thingsboard.io/pricing/)
- Bảng giới hạn Free chi tiết (chính thức): 5 dashboards, 5 users, 2 customers, 3 rule chains, 1 integration; mỗi tháng 0.5M transport messages, 1M transport data points, 3M rule engine executions, 100K JS executions, 200 alarms, 100 emails, 0 SMS; TTL time-series mặc định 30 ngày, alarm 30 ngày, RPC 30 ngày — [ThingsBoard Cloud subscription plans](https://thingsboard.io/docs/paas/reference/subscriptions/)
- Rate limit theo thiết bị (Free): telemetry messages 50/s, 600/phút, 7K/giờ; telemetry data points 100/s, 2K/phút, 15K/giờ. Rate limit REST tenant 100/s, 1K/phút — [ThingsBoard Cloud subscription plans](https://thingsboard.io/docs/paas/reference/subscriptions/)
- Không tìm thấy gói "Maker" trên trang giá Cloud; "Maker" là gói của bản tự host Professional Edition (nguồn bên thứ ba) — [ThingsBoard pricing](https://thingsboard.io/pricing/); [wz-it.com (bên thứ ba)](https://wz-it.com/en/knowledge/iot/thingsboard-pricing/)

**ThingSpeak (MathWorks)**
- Free license: 3 triệu message/năm, cập nhật tối đa mỗi 15 giây, 4 channels, chia sẻ riêng tư tối đa 3, MATLAB compute timeout 20 s; được mô tả là "For time-limited commercial evaluation of the service". MQTT subscriptions chỉ được nêu như quyền lợi của license Standard. Không có giá student công khai, phải liên hệ — [ThingSpeak license page](https://thingspeak.mathworks.com/prices/thingspeak_standard)

**Adafruit IO**
- Free: 30 data points/phút, 10 feeds, 5 dashboards, 5 groups, lưu 30 ngày; IO+ là 60 data points/phút. Mọi CREATE/UPDATE/DELETE dữ liệu đều tính vào rate limit; ngoài ra 300 HTTP request/phút mỗi user. Vượt rate → thông báo ở topic MQTT `{username}/throttle` — [Adafruit IO HTTP API docs](https://io.adafruit.com/api/docs/http.html); [Welcome to Adafruit IO](https://learn.adafruit.com/welcome-to-adafruit-io?view=all). Không tìm được tài liệu ghi ngày 2026; một số trang cũ còn ghi 60 dp/phút hoặc 5 feeds (đã lỗi thời).

**Grafana Cloud Free**
- 10k active series/tháng, giữ metrics 14 ngày, logs 50 GB/tháng giữ 14 ngày, tối đa 3 users; Pro từ $19/tháng — [Grafana pricing](https://grafana.com/pricing/); [Grafana Cloud Free](https://grafana.com/products/cloud/free-tier). Con số cũ "15k series, 13 tháng" năm 2021 đã lỗi thời — [TechCrunch 2021](https://techcrunch.com/?p=2094923)

**InfluxDB Cloud Serverless (Free Plan)**
- Giữ dữ liệu 30 ngày; ghi tối đa 5 MB mỗi 5 phút (~17 kB/s); vượt → HTTP 429; không ghi được dữ liệu lịch sử cũ hơn 30 ngày nếu không nâng cấp — [InfluxDB Cloud Serverless limits](https://docs.influxdata.com/influxdb3/cloud-serverless/admin/billing/limits/)

**HiveMQ Cloud Serverless (Free)**
- 100 kết nối đồng thời, 10 GB traffic/tháng, không SLA, không có Control Center, không migrate được từ Serverless sang gói khác — [HiveMQ FAQ (Atlassian)](https://hivemq.atlassian.net/wiki/spaces/HCSP/pages/2550988855/What+happens+when+the+limit+of+connections+is+reached); [HiveMQ blog Serverless vs Starter](https://hivemq.com/blog/hivemq-cloud-serverless-vs-starter-mqtt-iot-or-iiot)
- Retained messages trên cụm free hết hạn sau 3 ngày — [HiveMQ community](https://community.hivemq.com/t/free-pay-as-you-go-data-retention-retained-messages/2238)
- **Lưu ý mâu thuẫn:** trang giá hiện tại hivemq.com/pricing (đọc 16/11/2026) KHÔNG hiển thị gói Serverless Free, chỉ thấy Starter (từ $0.34/giờ, trial 15 ngày) và bản tự host "Lab" miễn phí 25 kết nối, license 30 ngày — [HiveMQ pricing](https://www.hivemq.com/pricing/). Không tìm thấy thông báo ngừng gói Free; cần đăng nhập console để xác nhận còn tạo được cụm Serverless miễn phí.

**EMQX Cloud Serverless**
- Free mỗi tháng: 1 triệu session minutes, 1 GB traffic, 1 triệu rule action executions; đặt spend limit = 0 thì chỉ dùng quota free; vượt quota thì deployment bị dừng; tối đa 1000 kết nối đồng thời — [EMQX pricing docs](https://docs.emqx.com/en/cloud/latest/price/pricing.html); [EMQX deploy FAQ](https://docs.emqx.com/en/cloud/latest/faq/deploy.md)
- 1 client kết nối liên tục ≈ 43.200 session minutes/tháng → quota đủ ~23 client — [EMQX blog](https://www.emqx.com/en/blog/how-to-get-a-forever-free-serverless-mqtt-service)

**Streamlit Community Cloud**
- "All apps without traffic for 12 hours go to sleep"; ai có quyền xem đều đánh thức được. Tài nguyên (số liệu 02/2024): CPU 0.078–2 cores, RAM 690 MB–2.7 GB, storage tối đa 50 GB — [Streamlit: Manage your app](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)
- Không có gói trả phí giữ app thức 24/7; ngưỡng ngủ đã giảm từ 7 ngày → 24/72 giờ → 12 giờ — [Streamlit forum](https://discuss.streamlit.io/t/web-apps-keeps-on-sleeping-after-30-minutes-or-a-day-of-inactivity/97350); [Streamlit forum](https://discuss.streamlit.io/t/how-to-prevent-the-app-enter-the-sleep-mode/87959)
- Giới hạn deploy: tối đa 5 lần cập nhật từ GitHub/phút; danh sách IP đi ra có thể đổi không báo trước — [Streamlit status/limits](https://docs.streamlit.io/deploy/streamlit-community-cloud/status)

**Hugging Face Spaces**
- Space free "cpu-basic" ngủ sau 48 giờ không hoạt động, không chỉnh được thời gian ngủ trên tier free — [HF Hub: manage spaces](https://huggingface.co/docs/huggingface_hub/main/guides/manage-spaces). Có một bài phàn nàn trên diễn đàn nói tier CPU Basic free bị thay đổi/hạn chế — chưa xác minh được — [HF forum](https://discuss.huggingface.co/t/official-community-complaint-revert-free-cpu-basic-spaces-and-remove-anti-developer-sdk-restrictions/177703)

**Render (free web service)**
- Tự tắt sau 15 phút không có traffic, cold start ~30–50+ giây; WebSocket trên free bị báo ngắt — [Render community](https://community.render.com/t/cold-boot-start-of-the-server-for-first-request/15911); [Render community](https://community.render.com/t/render-free-node-js-websocket-disconnects-for-no-reason/21750)

**Supabase Free**
- 2 project active, 500 MB DB/project, 1 GB file storage, 5 GB egress; project free bị pause sau 1 tuần không hoạt động, phải khôi phục thủ công — [Supabase pricing](https://supabase.com/pricing); vượt 500 MB → read-only (nguồn thứ cấp) — [automationatlas.io](https://automationatlas.io/answers/supabase-free-tier-limits-2026/)

**Blynk IoT / Arduino Cloud / Ubidots STEM**
- Blynk Free: 5 devices, 1 user, lịch sử 1 tuần (nguồn tổng hợp + blog Blynk) — [Blynk blog](https://blynk.io/blog/introducing-production-tier-more-room-for-growing-deployments); [Blynk pricing](https://blynk.io/pricing)
- Arduino Cloud Free: tối đa 2 devices (chính thức); bên thứ ba ghi giữ dữ liệu 1 ngày, 5 biến/Thing — [Arduino Cloud plans](https://cloud.arduino.cc/plans/); [toolradar (bên thứ ba)](https://toolradar.com/tools/arduino-cloud/pricing)
- Ubidots STEM: chỉ phi thương mại; ~3 thiết bị miễn phí, 10 biến/thiết bị (changelog 2021, chưa thấy nguồn 2026) — [Ubidots changelog](https://docs.ubidots.com/changelog/ubidots-stem-is-officially-our-educational-platform)

### Inferences
- **Tính lưu lượng cho ThingsBoard Free:** nếu đẩy mẫu 10 s với ~8 trường (pm1, pm25, pm10, temp, hum, lux, mq135_do, fan) → 8.640 msg/ngày ≈ 259K msg/tháng (dưới 0.5M) nhưng ≈ 2.07M data points/tháng → **vượt 1M**. Đẩy trung bình 1 phút: ≈ 345K dp/tháng; đẩy bản ghi 5 phút: ≈ 69K dp/tháng. → Nên gửi mỗi 1 phút (hoặc 5 phút + giá trị tức thời khi đổi trạng thái quạt/cảnh báo).
- ThingSpeak free bị chặn 15 s/lần cập nhật và 8 field/channel; 3M msg/năm ≈ 8.200/ngày → mẫu 10 s không vừa, mẫu 1 phút vừa. Có nhắc trong slide môn học nên "đúng bài" nhưng lệnh xuống thiết bị (TalkBack) yếu hơn ThingsBoard.
- Adafruit IO 30 dp/phút: mỗi trường là 1 data point → 8 trường mỗi 1 phút = 8 dp/phút, vừa; nhưng chỉ 10 feeds, lưu 30 ngày.
- Xively và SkyNET (nhắc trong slide) đã ngừng hoạt động từ lâu (theo hiểu biết chung; không kiểm tra lại trong lượt này).

### Gaps
- Không kiểm tra được trực tiếp: Firebase Spark (Realtime DB/Firestore), Fly.io, Railway, Node-RED hosting (FlowFuse), Grafana Live/MQTT datasource trên Grafana Cloud — không đủ lượt tra cứu; cần xác minh riêng nếu muốn xét.
- HiveMQ Serverless Free có còn đăng ký mới không: trang giá không còn hiển thị, nhưng không có thông báo ngừng.
- ThingsBoard Free có xoá tài khoản không hoạt động không: không tìm thấy quy định.

---

## 2. Streamlit Community Cloud: ngủ, MQTT, secrets, kết nối ngoài — chuyển nguyên `server_app.py` lên thì hỏng gì?

### Takeaway
Streamlit Cloud chạy được app Python + đọc secrets, nhưng app ngủ sau 12 giờ không có người xem và không có nơi lưu bền, nên **không thể dùng nó làm "người thu dữ liệu"**. Muốn dùng Streamlit làm dashboard hosted thì Pi phải đẩy dữ liệu vào một kho cloud (ThingsBoard/Supabase/InfluxDB/broker có retain), còn Streamlit chỉ đọc khi có người mở trang.

### Cited Findings
- App ngủ sau 12 giờ không có traffic; không có tier trả phí để giữ thức — [Streamlit docs](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app); [Streamlit forum](https://discuss.streamlit.io/t/how-to-prevent-the-app-enter-the-sleep-mode/87959)
- Workaround không chính thức: commit rỗng mỗi 10 giờ, hoặc GitHub Actions + Selenium/Playwright bấm nút đánh thức; GET HTTP thường trả 200 nhưng không đánh thức app — [dev.to Selenium](https://dev.to/virgoalpha/keeping-your-streamlit-app-awake-using-selenium-and-github-actions-4ajd); [dev.to Playwright](https://dev.to/yasumorishima/http-200-but-your-streamlit-app-is-still-sleeping-why-get-requests-dont-work-and-how-playwright-3g67)
- Có báo cáo app kẹt ở "Spinning up manager process…" sau khi đánh thức — [Streamlit forum](https://discuss.streamlit.io/t/all-apps-on-my-account-stuck-at-startup-after-waking-from-sleep/122744)
- Secrets: dán nội dung `secrets.toml` vào ô Secrets trong Advanced settings, đọc bằng `st.secrets`; không commit `secrets.toml` — [Streamlit secrets management](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)
- Luồng nền (paho `loop_start`) không được gọi hàm Streamlit/`st.session_state` — lỗi "missing ScriptRunContext"; cách làm: lưu vào biến toàn cục/đối tượng dùng chung rồi để script rerun đọc — [Streamlit multithreading docs](https://docs.streamlit.io/develop/concepts/design/multithreading); [Streamlit forum MQTT](https://discuss.streamlit.io/t/using-streamlit-with-mqtt-forced-multithreading/121708)
- IP đi ra của Community Cloud được liệt kê (18 IP) nhưng có thể đổi — tức là có kết nối ra ngoài, nhưng không nên whitelist cố định — [Streamlit status](https://docs.streamlit.io/deploy/streamlit-community-cloud/status)

### Inferences
- Những gì hỏng nếu đẩy nguyên `server_app.py`:
  1. `BROKER = "localhost"` — trên cloud không có Mosquitto; phải đổi sang broker cloud có TLS (HiveMQ/EMQX, cổng 8883) hoặc bỏ MQTT, đọc từ DB cloud.
  2. Ghi CSV vào `C:\mqtt\data` — đường dẫn Windows, filesystem container là tạm thời, mất khi app ngủ/khởi động lại → mất lịch sử.
  3. Khi app ngủ, không có subscriber → mọi message trong lúc ngủ bị mất (trừ message retained cuối cùng); dashboard mở ra sẽ trống lịch sử.
  4. Mật khẩu MQTT phải chuyển từ `.env` sang `st.secrets`.
- Kết nối TLS ra Postgres (Supabase) hoặc MQTT 8883 từ Streamlit Cloud là kết nối ra ngoài thông thường; tài liệu không nêu chặn cổng — chưa thấy nguồn chính thức khẳng định/ phủ định cổng 8883 (xem Gaps).

### Gaps
- Không tìm thấy tài liệu chính thức nói rõ Streamlit Cloud có chặn cổng ra (vd 8883, 5432) hay không; cộng đồng có dùng nhưng chưa trích được nguồn.
- Không xác minh được cơ chế "private app/giới hạn người xem" trên trang secrets.

---

## 3. Kênh lệnh xuống (downlink) tới Pi sau NAT, và độ đơn giản của client Python

### Takeaway
Mọi giải pháp dựa trên MQTT đều xuyên NAT được vì Pi chủ động kết nối ra và subscribe. ThingsBoard có sẵn cả widget nút bấm/công tắc trên dashboard gửi RPC hoặc shared attribute xuống thiết bị — đây là đường ngắn nhất để có "dashboard điều khiển quạt" mà không viết backend.

### Cited Findings
- ThingsBoard MQTT: thiết bị publish `v1/devices/me/telemetry`, dùng access token làm username, mật khẩu để trống; 1883 thường, 8883 TLS (xác thực server bằng CA root). Nhận shared attribute: subscribe `v1/devices/me/attributes`. Nhận RPC từ server: subscribe `v1/devices/me/rpc/request/+`, trả lời ở `v1/devices/me/rpc/response/$id` — [ThingsBoard MQTT API](https://thingsboard.io/docs/reference/mqtt-api/)
- Telemetry chấp nhận timestamp phía thiết bị `{"ts": <epoch ms>, "values": {...}}` và mảng nhiều bản ghi trong 1 message (hữu ích để đẩy bù dữ liệu offline); không có `ts` thì server tự gán — [ThingsBoard telemetry](https://thingsboard.io/docs/user-guide/telemetry/)
- RPC trên Free có TTL 30 ngày, không thấy giới hạn số lượng riêng — [ThingsBoard subscriptions](https://thingsboard.io/docs/paas/reference/subscriptions/)
- HiveMQ/EMQX: broker MQTT chuẩn, hỗ trợ MQTT over TLS và WebSocket — [HiveMQ pricing](https://www.hivemq.com/pricing/); [EMQX pricing docs](https://docs.emqx.com/en/cloud/latest/price/pricing.html)
- Adafruit IO có MQTT (thiết bị subscribe feed để nhận lệnh, giới hạn chung 30 dp/phút) — [Welcome to Adafruit IO](https://learn.adafruit.com/welcome-to-adafruit-io?view=all)
- ThingSpeak free: MQTT subscriptions chỉ ghi là quyền lợi Standard license — [ThingSpeak license](https://thingspeak.mathworks.com/prices/thingspeak_standard)

### Inferences
- Phía Pi chỉ cần thêm `paho-mqtt` (đã có trong repo) với `tls_set()` + cổng 8883 + token; không cần mở port, không cần SDK riêng.
- Nếu chọn Streamlit + broker cloud, nút bấm trong Streamlit publish lên topic lệnh (ví dụ `nha/pi01/cmd`), Pi subscribe. Nhưng cần tự làm xác thực người bấm (Streamlit app public mặc định) → nhiều việc hơn ThingsBoard.
- Lệnh từ cloud nên đi qua lớp ghi đè thủ công đã có (`/fan on|off`, hết hạn 2 giờ) chứ không ghi trực tiếp GPIO, để giữ nguyên luật an toàn cục bộ.

### Gaps
- Chưa kiểm tra Supabase Realtime/Firebase listener trên Pi (Python) cho downlink — không đủ lượt tra cứu.

---

## 4. Bảo mật cơ bản

### Takeaway
Dùng TLS (8883), mỗi thiết bị một credential, giữ token trong `.env`/`st.secrets`; chia dashboard công khai chỉ-xem và phần điều khiển cần đăng nhập.

### Cited Findings
- ThingsBoard: mỗi device có access token riêng làm username MQTT; hỗ trợ TLS 8883 — [ThingsBoard MQTT API](https://thingsboard.io/docs/reference/mqtt-api/); có Customers (2 trên Free) và Users (5) để phân quyền — [ThingsBoard subscriptions](https://thingsboard.io/docs/paas/reference/subscriptions/)
- Streamlit: secrets qua ô Secrets, không commit `secrets.toml` — [Streamlit secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)

### Inferences
- ThingsBoard cho phép dashboard "public" (chỉ xem) và tài khoản customer có quyền bấm nút — đáp ứng "read-only public vs authenticated control" mà không viết code xác thực. (Tính năng public dashboard của ThingsBoard là kiến thức chung, chưa trích trang tài liệu cụ thể.)
- Không để token trong repo hay trong URL; với Streamlit, token broker nằm ở server-side nên viewer không thấy, nhưng ai mở link cũng bấm được nút nếu không tự thêm đăng nhập.

### Gaps
- Chưa trích được trang tài liệu ThingsBoard về public dashboard và quyền widget điều khiển cho customer user.

---

## 5. Mẫu xử lý mất mạng (buffer + gửi bù)

### Takeaway
Giữ SQLite là nguồn sự thật trên Pi; một tiến trình "uplink" đọc các bản ghi chưa gửi (theo con trỏ `ts` lưu trong bảng `state`) và đẩy lên cloud kèm `ts` gốc, giống mô hình `outbox` đang có cho Telegram.

### Cited Findings
- ThingsBoard nhận mảng `{ts, values}` trong một message, dùng để đẩy dữ liệu offline theo lô — [ThingsBoard telemetry](https://thingsboard.io/docs/user-guide/telemetry/)
- Rate limit Free theo thiết bị: 600 telemetry msg/phút, 2K data points/phút, 15K dp/giờ — giới hạn tốc độ backfill — [ThingsBoard subscriptions](https://thingsboard.io/docs/paas/reference/subscriptions/)
- InfluxDB Free không cho ghi dữ liệu cũ hơn 30 ngày — [InfluxDB limits](https://docs.influxdata.com/influxdb3/cloud-serverless/admin/billing/limits/)
- HiveMQ free: retained message hết hạn sau 3 ngày — broker không phải kho lưu — [HiveMQ community](https://community.hivemq.com/t/free-pay-as-you-go-data-retention-retained-messages/2238)

### Inferences
- Backfill nên gửi bản ghi 5 phút (bảng `readings`) theo lô ≤ ~100 bản ghi × 8 trường để nằm dưới 2K dp/phút; sau 1 ngày mất mạng (288 bản ghi × 8 = ~2.300 dp) đẩy xong trong vài phút.
- MQTT QoS 1 + chỉ cập nhật con trỏ khi nhận PUBACK là đủ cho đồ án; không cần persistent session phía broker.

### Gaps
- Không có nguồn đo thực tế về độ trễ/độ tin cậy backfill trên ThingsBoard Free.

---

## 6. Gotcha thực tế được người dùng báo cáo

### Takeaway
Rủi ro lớn nhất khi demo: app ngủ (Streamlit 12 giờ, HF 48 giờ, Render 15 phút) và DB bị pause (Supabase 1 tuần). Nền tảng IoT chuyên dụng (ThingsBoard, Adafruit IO) không ngủ nhưng có rate limit cứng.

### Cited Findings
- Streamlit: app ngủ cả khi repo được push hằng ngày; GitHub Actions 12 giờ/lần vẫn không đủ; kẹt khởi động sau khi thức — [forum](https://discuss.streamlit.io/t/streamlit-app-hibernating-even-after-daily-repo-pushes/97582); [forum](https://discuss.streamlit.io/t/all-apps-on-my-account-stuck-at-startup-after-waking-from-sleep/122744)
- Render free: cold start 30–60 s, WebSocket ngắt — [Render community](https://community.render.com/t/cold-boot-start-of-the-server-for-first-request/15911)
- Supabase: pause sau 1 tuần không hoạt động, app "tối" cho tới khi khôi phục tay — [Supabase pricing](https://supabase.com/pricing); [jetadmin (bên thứ ba)](https://www.jetadmin.io/blog/supabase-pricing-2026-guide-to-plans-limits-and-real-world-costs/)
- EMQX Serverless: hết quota là deployment bị dừng (không tính tiền nếu spend limit = 0) — [EMQX FAQ](https://docs.emqx.com/en/cloud/latest/faq/deploy.md)
- Adafruit IO: vượt 30 dp/phút bị throttle — [Adafruit IO API](https://io.adafruit.com/api/docs/http.html)
- HiveMQ: không migrate được từ Serverless — [HiveMQ blog](https://hivemq.com/blog/hivemq-cloud-serverless-vs-starter-mqtt-iot-or-iiot)

### Inferences
- Pi ghi dữ liệu liên tục vào Supabase sẽ được tính là "hoạt động" nên khó bị pause (suy luận, chưa có nguồn xác nhận hoạt động nào được tính).

### Gaps
- Chưa có báo cáo người dùng về ThingsBoard Cloud Free xoá tài khoản/thiết bị không hoạt động.

---

## Bảng so sánh

| Ứng viên | Vai trò | Giới hạn free chính | Ngủ / hết hạn | Thẻ | Lệnh xuống Pi | Công sức (1–2 ngày?) |
|---|---|---|---|---|---|---|
| **ThingsBoard Cloud Free** | Kho dữ liệu + dashboard + điều khiển | 5 device, 1M dp/tháng, 0.5M msg/tháng, TTL 30 ngày, 600 msg/phút/device | Không ngủ, "never expires" | Không | Có: RPC, shared attributes, widget nút/công tắc | Có — chỉ thêm paho TLS trên Pi, dashboard kéo-thả |
| ThingSpeak Free | Kho + biểu đồ | 3M msg/năm, ≥15 s/lần, 4 channels, 8 field | Không ngủ; "time-limited commercial evaluation" | Không (rõ ràng chưa xác minh) | Yếu (MQTT subscribe thuộc Standard) | Có, rất dễ (HTTP REST); có trong slide |
| Adafruit IO Free | Kho + dashboard | 30 dp/phút, 10 feeds, 5 dashboards, 30 ngày | Không ngủ | Không | Có (MQTT feed) | Có |
| Streamlit Community Cloud | Host dashboard | 0.078–2 CPU, ≤2.7 GB RAM | Ngủ sau 12 giờ, FS tạm | Không | Chỉ khi ghép broker; tự làm xác thực | Có nếu đã có kho cloud; tái dùng code Streamlit |
| HF Spaces (cpu-basic) | Host dashboard | — | Ngủ sau 48 giờ | Không | Như Streamlit | Có |
| Render free | Host web app | — | Ngủ sau 15 phút, cold start 30–60 s | (chưa xác minh) | Như trên | Trung bình |
| HiveMQ Cloud Serverless | Broker MQTT | 100 kết nối, 10 GB/tháng, retain 3 ngày | Không rõ; gói free không còn trên trang giá | Không rõ | Có (MQTT) | Có, nhưng cần thêm kho dữ liệu |
| EMQX Serverless | Broker MQTT | 1M session-min, 1 GB, 1M rule actions/tháng | Dừng khi hết quota | Không rõ | Có (MQTT) | Có, cần thêm kho |
| InfluxDB Cloud Free | Kho time-series | 30 ngày, 5 MB/5 phút | — | — | Không | Có + Grafana |
| Grafana Cloud Free | Dashboard (+Prometheus/Loki) | 10k series, 14 ngày, 3 users | — | — | Không (thuần hiển thị) | Trung bình |
| Supabase Free | Postgres + Realtime | 500 MB, 2 project, 5 GB egress | Pause sau 1 tuần không hoạt động | — | Có thể (Realtime), chưa xác minh client Python | Trung bình |
| Blynk Free | Kho + app/dashboard | 5 device, 1 user, lịch sử 1 tuần | — | — | Có | Có (chủ yếu cho vi điều khiển) |
| Arduino Cloud Free | Kho + dashboard | 2 device (chính thức); giữ 1 ngày (bên thứ ba) | — | — | Có | Có, nhưng giới hạn chặt |
| Ubidots STEM | Kho + dashboard | ~3 device, 10 biến/device, phi thương mại | — | — | Có | Có |

## Đề xuất xếp hạng

1. **Khuyến nghị: Pi → ThingsBoard Cloud Free (MQTT TLS 8883) → dashboard ThingsBoard (có nút bật/tắt quạt qua RPC hoặc shared attribute).**
   - Một dịch vụ lo cả lưu trữ, dashboard hosted, phân quyền, lệnh xuống; không ngủ, không thẻ, không hết hạn.
   - Pi gửi trung bình 1 phút hoặc bản ghi 5 phút kèm `ts` từ SQLite (backfill được); ước tính < 0.35M dp/tháng, dưới 1M.
   - Lệnh từ dashboard đi vào cơ chế ghi đè thủ công có sẵn (hết hạn 2 giờ), luật tất định cục bộ vẫn quyết định cuối cùng.
   - Giữ dashboard Streamlit cục bộ cho phần phân tích/camera nếu muốn.
2. **Dự phòng: Pi → broker cloud (EMQX Serverless hoặc HiveMQ Serverless, TLS 8883, có retained message) + kho Supabase/ThingSpeak → Streamlit Community Cloud.**
   - Tái dùng nhiều code Streamlit; nhưng phải thay `localhost`, bỏ ghi CSV cục bộ, đọc lịch sử từ kho cloud, chuyển mật khẩu sang `st.secrets`, tự thêm đăng nhập cho nút điều khiển, và chấp nhận app ngủ sau 12 giờ (mở trước buổi demo vài phút).
   - Phương án tối giản nhất nếu chỉ cần "có dashboard online": ThingSpeak (đúng bài trong slide) — nhưng điều khiển ngược yếu.
