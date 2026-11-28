# Cơ cấu chấp hành bổ sung cho trạm AIoT (Raspberry Pi 4)

Phạm vi: chọn MỘT cơ cấu chấp hành thêm vào trạm hiện có (DHT11, BH1750, PMS7003, relay + quạt 5V trên GPIO23, camera USB ban công; MQ135 bị bỏ nên trống GPIO26 / pin 37 và một chân 5V). Trạng thái tính đến 16/11/2026, Raspberry Pi OS Bookworm trở lên.

## Bảng xếp hạng và khuyến nghị (cơ cấu nào khép vòng tốt nhất với cảm biến và AI hiện có?)

### Takeaway
Khuyến nghị số 1 là **một servo SG90/MG90S kéo cửa thông gió (vent damper) hoặc mái che mô hình trên ban công**. Servo được điều khiển bằng các quyết định đã có sẵn (PM2.5 + dự báo đối chứng, cảnh báo mưa/nắng), cấp nguồn 5V riêng, nối chung GND, và tín hiệu đi từ GPIO26 hoặc GPIO12. Đây là phương án duy nhất có chuyển động cơ học thấy rõ, ăn khớp với cả hai nhánh AI (chất lượng không khí và thời tiết ban công), và mua được với giá 42.000–85.000 ₫. Phương án dự phòng rủi ro thấp là còi + LED RGB, nhưng nó yếu hơn về mặt học thuật (là cơ cấu báo hiệu, không có chuyển động).

### Cited Findings
Danh sách ứng viên (cột "lý do" là suy luận của người viết từ các nguồn bên dưới):

| Hạng | Phương án | Hành động vật lý | Quyết định điều khiển nó | Nguồn và giao tiếp | Giá VN |
|---|---|---|---|---|---|
| 1 | Servo SG90 (hoặc MG90S bánh răng kim loại) kéo cửa thông gió hoặc mái che | Đóng/mở cửa gió hoặc kéo mái che | Đóng khi PM2.5 ngoài trời xấu hoặc dự báo đối chứng TB 3 giờ > ngưỡng (đồng thời bật quạt lọc, chế độ "tuần hoàn trong"); đóng hoặc kéo mái che khi `weather.py` báo mưa; mở lại khi tốt | 1 GPIO tín hiệu PWM 50 Hz; nguồn 5V riêng ≥ 1 A, chung GND | SG90 42.000 ₫, SG90+PRO 55.000 ₫, MG90S 85.000 ₫ ([Hshop](https://hshop.vn/search?q=servo+sg90)) |
| 2 | Còi chủ động + LED RGB (hoặc dải WS2812) báo AQI tại chỗ | Màu theo nhóm AQI, còi khi khẩn cấp | Nhóm AQI, chế độ khẩn cấp, mất cảm biến | 3–4 GPIO, qua điện trở hoặc transistor | Không tìm được giá Hshop; chỉ có giá linh kiện công nghiệp (xem Gaps) |
| 3 | Kênh relay thứ 2 (quạt hút thứ hai, đèn, máy phun sương USB) | Bật/tắt tải thứ hai | Ví dụ quạt hút khi trời đẹp, quạt lọc khi trời xấu | 1 GPIO, dùng lại thiết kế relay đang chạy | Chỉ có giá hàng nhập (gói 6 cái ~595.831 ₫ trên [Ubuy VN](https://www.ubuy.vn/vi/product/C1VQYO3SK-hosyond-4pack-dc-5v-4-channel-relay-module-board-with-optocoupler-isolation-high-or-low-level-trigger-for-arduino-raspberry-pi)); giá bán lẻ trong nước chưa xác minh |
| 4 | Động cơ bước 28BYJ-48 + ULN2003 mở cửa sổ | Mở/đóng chậm, đặt được vị trí | Giống phương án 1 | 4 GPIO + nguồn 5V riêng | Chưa xác minh |
| 5 | Máy phun sương / tạo ẩm siêu âm 5V qua MOSFET hoặc relay | Tăng độ ẩm | DHT11 báo độ ẩm thấp | 1 GPIO + MOSFET logic-level | Chưa xác minh |

- Servo trong gpiozero cần đúng một chân. `value = None` thả servo về trạng thái "uncontrolled" (ngừng phát xung, xoay tay được). — [gpiozero Output API](https://gpiozero.readthedocs.io/en/stable/api_output.html)
- gpiozero có sẵn các lớp `Buzzer` (chỉ bật/tắt), `TonalBuzzer`, `RGBLED` (PWM mặc định) và `Motor` (H-bridge), nên các phương án 2–4 không cần thư viện mới. — [gpiozero Output API](https://gpiozero.readthedocs.io/en/stable/api_output.html)
- SG90 có điện áp định mức 4,8–6 V, dòng nghỉ ~10 mA, dòng khi chạy ~100–250 mA, dòng kẹt (stall) ~360 mA (đo thực tế) đến ~550–650 mA tùy nguồn. — [ProtoSupplies SG90](https://protosupplies.com/product/servo-motor-micro-sg90/); [Circuit Rocks SG90](https://docs.circuit.rocks/motors/sg90-servo/); [ServoDatabase Yahboom SG90](https://servodatabase.com/servo/yahboom/sg90)

### Inferences
- **Vì sao chọn servo làm số 1:** một quyết định ("không khí ngoài trời xấu hoặc sắp mưa") kéo theo hai hành động phối hợp: đóng cửa gió bằng servo và bật quạt lọc bằng relay hiện có. Đây chính là vòng "cảm biến → AI/luật → hành động vật lý" mà giảng viên muốn thấy. Camera ban công còn chụp được ảnh cửa đã đóng để gửi kèm tin Telegram làm bằng chứng.
- **Rủi ro:** nhóm đã từng bỏ mô hình cửa servo vì phức tạp. Để giảm rủi ro thì giữ servo ở dạng 2 vị trí (mở 0°, đóng 90°), không cần công tắc hành trình hay phản hồi vị trí, và dùng chung luật tất định với quạt. Gemini vẫn không điều khiển thiết bị. Hai lỗi cũ "cửa kẹt" và `/door` nên giữ ngoài phạm vi; chỉ cần một lệnh ghi đè, ví dụ `/vent open|close`.
- **Kịch bản demo khoảng 2 phút:** (1) Chế độ demo 20 s. (2) Đưa que hương hoặc khói nhỏ lại gần PMS7003: PM2.5 vượt ngưỡng, servo đóng cửa gió, quạt bật, Telegram nhận tin kèm ảnh. (3) Che BH1750 và thổi hơi ẩm vào DHT11 để giả lập mưa: mái che kéo ra. (4) Không khí sạch trở lại: sau thời gian chờ demo, quạt tắt và cửa mở. Hysteresis cho thấy hệ thống không nhảy trạng thái.
- **Phương án 2 (còi + LED)** dễ nhất và gần như không rủi ro về điện, nhưng yếu về học thuật (xem mục định nghĩa). Có thể thêm một LED nhỏ như phụ kiện đi kèm servo nếu còn thời gian.
- **Phương án 3 (relay thứ 2)** lặp lại cái đã có, nên khó gây ấn tượng "tie together" hơn quạt hiện tại.
- **Phương án 4 (28BYJ-48)** tốn 4 GPIO và chậm hơn, không có lợi thế rõ so với servo cho cơ cấu 2 vị trí.
- **Phương án 5 (phun sương)** đưa nước lại gần mạch. Độ ẩm Hà Nội thường cao nên luật "độ ẩm thấp → phun" hiếm khi kích hoạt khi demo, và hơi ẩm còn làm PMS7003 đọc cao. Vì vậy không khuyến nghị.

### Gaps
- Không tìm được giá bán lẻ trong nước (Hshop, Linh Kiện 3M, Shopee) cho relay 2 kênh, module còi, 28BYJ-48 + ULN2003 và máy phun sương USB. Theo trí nhớ thì đều rẻ (vài chục nghìn đồng), nhưng chưa xác minh, nhóm cần tự tra.
- Chưa có số liệu dòng tiêu thụ của 28BYJ-48 từ datasheet.

## An toàn điện trên Pi 4 (dòng 5V, giới hạn GPIO 3,3V, nguồn riêng, diode, GND chung)

### Takeaway
GPIO của Pi chỉ chịu 3,3 V, tối đa 16 mA/chân và 50 mA tổng cho cả các chân. Pi 4 chỉ cấp 1,2 A tổng cho ngoại vi USB với bộ nguồn 3 A. Chân 5V nối thẳng vào nguồn vào và đi vòng qua mạch bảo vệ/giám sát. Vì vậy servo (dòng kẹt tới ~0,5–0,65 A, lại đang dùng chung 5V với quạt và PMS7003) nên lấy nguồn 5V riêng, nối chung GND với Pi, có tụ lọc, và GPIO chỉ cấp tín hiệu.

### Cited Findings
- "Raspberry Pi GPIO pins operate at 3.3 V and aren't 5 V tolerant." Một chân GPIO cấp tối đa 16 mA, tổng các chân tối đa 50 mA. Tài liệu đồng thời ghi Pi 4 (BCM2711) có drive strength mặc định 4 mA, tối đa 8 mA, và chưa giải thích sự chênh lệch giữa các con số này. — [Raspberry Pi documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html)
- Nguồn 3,3 V được thiết kế cho khoảng ~3 mA mỗi chân GPIO, và sẽ "collapse" nếu mọi chân cùng tải 16 mA. — [Raspberry Pi documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html)
- Pi 4 Model B: nguồn khuyến nghị 3,0 A, tổng dòng ngoại vi USB tối đa 1,2 A, dòng tiêu thụ điển hình của bo trần khi hoạt động 600 mA. Thiết bị nối thẳng vào 5V "bypasses the PMIC", nên Pi không đo được dòng đó. — [Raspberry Pi documentation, bảng nguồn](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html)
- Tài liệu chính thức không công bố dòng tối đa qua các chân 5V của header. Con số ~2,5 A chỉ là thông tin cộng đồng. — [Techsparx](https://techsparx.com/linux-sbc/raspberry-pi/pi4-usb-power-limit.html); [RetroPie forum](https://retropie.org.uk/forum/post/165614)
- Pi không cấp cho ngoại vi quá dòng thiết kế của nó, dù bộ nguồn lớn đến đâu. — [Kevin Boone](https://kevinboone.me/pi_waveshare_usb.html)
- Với servo: nên dùng nguồn 5V riêng, nối chung GND, và đặt tụ hóa ≥ ~100 µF giữa 5V và GND của servo để hấp thụ xung dòng. — [Circuit Rocks SG90](https://docs.circuit.rocks/motors/sg90-servo/); [ProtoSupplies SG90](https://protosupplies.com/product/servo-motor-micro-sg90/)

### Inferences
- **Ngân sách 5V hiện tại:** Pi (~600 mA) + PMS7003 + quạt + camera USB đã dùng chung rail. Thêm dòng kẹt servo ~0,5 A có thể gây sụt áp (`throttled` ≠ 0) và nhiễu PMS7003. Nên dùng một sạc USB 5V/2A rời hoặc module hạ áp, cắt một cáp USB lấy 5V/GND cấp cho servo, rồi nối GND của nguồn này với một chân GND của Pi (ví dụ pin 39 cạnh pin 37).
- **Tín hiệu:** dây tín hiệu servo chỉ kéo vài mA nên đi thẳng từ GPIO được, không cần transistor. Xung 3,3 V thường đủ cho SG90; nếu servo chạy không ổn định mới thêm level shifter. Đây là suy luận từ thực tế cộng đồng, chưa có nguồn chính thức.
- **Diode flyback:** servo có mạch điều khiển bên trong nên không cần diode ngoài; tụ lọc là đủ. Diode flyback (1N4007/1N5819 song song ngược với tải) bắt buộc khi dùng MOSFET hoặc transistor đóng cắt trực tiếp động cơ DC, bơm hay cuộn dây (phương án 5). Module relay bán sẵn thường đã có diode trên bo. Đây là kiến thức chung, chưa có trích dẫn trong ghi chú này.
- **Quy tắc dây:** tắt Pi và rút nguồn trước khi đổi dây (đúng với CLAUDE.md của dự án). Không cấp 5V vào bất kỳ chân GPIO nào.

### Gaps
- Chưa có nguồn chính thức về dòng tối đa của chân 5V header trên Pi 4 (cần đo hoặc hỏi diễn đàn Raspberry Pi).
- Chưa có trích dẫn chính thức về yêu cầu diode flyback cho module relay hoặc MOSFET (nên lấy từ Adafruit hoặc datasheet module khi viết báo cáo).

## Các vấn đề phần mềm đã biết trên Pi 4 (jitter PWM, RPi.GPIO trên Bookworm, gpiozero + lgpio 2025–2026)

### Takeaway
Trên Bookworm trở lên, gpiozero dùng lgpio làm pin factory mặc định. RPi.GPIO gốc không còn chạy vì kernel bỏ giao diện cũ; thay bằng gói tương thích `rpi-lgpio`. PWM servo qua lgpio là software-timed, tự tác giả lgpio cảnh báo servo sẽ "fidget". Với cơ cấu 2 vị trí, cách giảm nhẹ đơn giản nhất là quay tới vị trí rồi đặt `value = None` để ngừng phát xung. pigpio vẫn chạy trên Pi 4 nhưng gpiozero đã bỏ lời khuyên "dùng pigpio để hết jitter".

### Cited Findings
- gpiozero 2.0 (12/09/2023) đặt `LGPIOFactory` làm mặc định, `RPiGPIOFactory` xuống ưu tiên thứ hai. Bản mới nhất là 2.0.1.post3 (27/07/2026), "Removed warning about using pigpio to fix servo jitter" và sửa lỗi chọn sai gpiochip trên Pi 5. Không có bản phát hành nào năm 2025. — [gpiozero changelog](https://gpiozero.readthedocs.io/en/latest/changelog.html)
- Tài liệu pin của gpiozero ghi pigpio factory "(not Pi 5)". Có thể chọn factory bằng biến môi trường `GPIOZERO_PIN_FACTORY`. — [gpiozero API - Pins](https://gpiozero.readthedocs.io/en/stable/api_pins.html)
- `tx_servo` trong lgpio là "software timed servo pulses". Tác giả cảnh báo "The timing jitter will cause the servo to fidget", có thể làm servo nóng và nhanh mòn, và khuyên chỉ dùng để thử. Độ rộng xung 500–2500 µs, 40–500 xung/giây. `tx_pwm` cũng là software-timed, tối đa 10 kHz. — [lgpio Python docs](https://abyz.me.uk/lg/py_lgpio.html)
- Mặc định của gpiozero `Servo`: xung 1–2 ms, khung 20 ms. `AngularServo` mặc định −90…+90°, chỉ chính xác khi đặt đúng `min_angle`/`max_angle`. `TonalBuzzer` không chạy với PiGPIOFactory. — [gpiozero Output API](https://gpiozero.readthedocs.io/en/stable/api_output.html)
- Theo một trả lời trên diễn đàn, RPi.GPIO không chạy trên Bookworm vì kernel không còn cung cấp API liên quan. `rpi-lgpio` là gói tương thích giữ nguyên `import RPi.GPIO`, nhưng không cài chung được với `RPi.GPIO` trong cùng môi trường. — [DietPi forum](https://dietpi.com/forum/t/rpi-gpio-node-red-library-not-work-on-dietpi-bookworm-and-trixie/25047); [rpi-lgpio docs](https://rpi-lgpio.readthedocs.io/)
- Pin table chính thức liệt kê PWM0 trên GPIO12 (pin 32) và PWM1 trên GPIO13 (pin 33). — [Raspberry Pi documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html)

### Inferences
- Dự án đã dùng `lgpio`/`gpiozero` cho relay, nên servo qua `gpiozero.AngularServo` (factory lgpio) là ít thay đổi nhất. Cách tránh jitter: chỉ phát xung ~1 s khi đổi vị trí, sau đó `servo.value = None` (hoặc `detach()`). Cửa gió nhẹ nên giữ được vị trí nhờ ma sát bánh răng.
- Nếu cần xung ổn định liên tục thì có hai hướng: (a) đặt chân tín hiệu ở GPIO12 (pin 32), bật overlay PWM phần cứng và dùng sysfs hoặc thư viện hardware-PWM; (b) chạy daemon `pigpiod` (DMA-timed, vẫn chạy trên Pi 4) với `PiGPIOFactory`. Cả hai đều thêm độ phức tạp, nên chỉ dùng khi cách "phát xung rồi thả" không đủ. Cấu hình overlay cụ thể chưa được kiểm chứng trong ghi chú này.
- GPIO26 (pin 37, vừa trống sau khi bỏ MQ135) có GND ở pin 39 ngay cạnh, rất tiện đi dây. Dùng GPIO26 nếu chọn software PWM; chọn GPIO12 nếu muốn để ngỏ phương án PWM phần cứng.

### Gaps
- Chưa có nguồn chính thức (Raspberry Pi docs) về cú pháp `dtoverlay=pwm` / `pwm-2chan` cho Pi 4 trên Bookworm/Trixie, cũng như tình trạng gói `pigpio` trên Trixie. Cần kiểm tra trước khi chọn hướng PWM phần cứng.
- Chưa có số đo jitter định lượng của lgpio trên Pi 4.

## Định nghĩa và phân loại cơ cấu chấp hành trong giáo trình IoT

### Takeaway
Các nguồn định nghĩa cơ cấu chấp hành là bộ chuyển đổi (transducer) biến tín hiệu lệnh thành tác động vật lý lên quá trình, và thường nhấn mạnh chuyển động hoặc điều khiển cơ cấu. Theo nghĩa đó, servo và relay + quạt là cơ cấu chấp hành rõ ràng. Còi và LED nằm ở vùng ranh giới: theo nghĩa rộng "tạo tác động vật lý" (âm thanh, ánh sáng) thì vẫn tính, nhưng chúng không tác động lên đại lượng được giám sát.

### Cited Findings
- "The actuator is a transducer that transforms a command signal into a physical action on the process." — [Hands-On Industrial Internet of Things (O'Reilly/Packt)](https://www.oreilly.com/library/view/hands-on-industrial-internet/9781789537222/52d82ea2-6cd7-44f0-81c0-b5ebaf8e932f.xhtml)
- IEEE: cơ cấu chấp hành là thành phần điều khiển hoặc di chuyển cơ cấu trong thiết bị IoT; khác với cảm biến (giám sát môi trường), nó "helps create movement". — [IEEE Blended Learning Program](https://blp.ieee.org/what-is-actuator-in-iot/)
- Giáo trình Bahga & Madisetti, *Internet of Things: A Hands-On Approach* (2014), được nhiều đề cương dùng làm sách chính. Ghi chú bài giảng dựa trên sách mô tả thiết bị IoT là thiết bị có định danh duy nhất, "can perform remote sensing, actuating and monitoring". — [Sethu Institute syllabus](https://www.sethu.ac.in/dept/ECE/courseware/19UEC603.pdf); [Scribd IoT notes](https://www.scribd.com/document/809494969/IoT-Unit-wise-Notes)

### Inferences
- Trong báo cáo nên gọi servo + quạt là cơ cấu chấp hành tác động lên môi trường (actuator theo nghĩa hẹp). Nếu thêm còi/LED thì gọi là "cơ cấu báo hiệu / chấp hành cảnh báo" (indicator), tránh để giảng viên bắt bẻ.
- Vòng kín thực sự: quạt bật làm PM2.5 trong hộp giảm, PMS7003 đo lại được. Cửa gió đóng cũng tác động lên PM2.5 trong hộp mô hình. Còi/LED thì không đóng vòng vật lý.

### Gaps
- Không truy cập được nguyên văn chương "sensing and actuation" của Bahga & Madisetti. Định nghĩa ở trên lấy từ nguồn thứ cấp; nhóm nên trích trực tiếp từ sách nếu có bản giấy hoặc PDF của thư viện.
