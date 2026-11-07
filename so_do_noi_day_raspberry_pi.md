# SƠ ĐỒ ĐẤU NỐI CẢM BIẾN VỚI RASPBERRY PI 4 MODEL B

*DHT11 – BH1750 – PMS7003 – MQ135 (chân DO) – Relay + quạt (qua breadboard)*

## 1. Tổng quan

Tài liệu mô tả cách nối dây giữa Raspberry Pi 4 Model B (header 40 chân, đánh số theo vị trí vật lý) và các thiết bị thông qua breadboard. Hệ thống gồm 5 thiết bị: DHT11 (nhiệt độ, độ ẩm), BH1750 (cường độ ánh sáng), PMS7003 (bụi mịn), MQ135 (chất lượng không khí, chỉ đọc chân digital DO) và quạt 5V điều khiển qua module relay.

## 2. Các chân Raspberry Pi được sử dụng

| Pin vật lý | Chức năng | GPIO    | Dùng cho                  |
| ---------- | --------- | ------- | ------------------------- |
| Pin 1      | 3.3V      | –       | Rail trái (+)             |
| Pin 2      | 5V        | –       | Rail phải (+)             |
| Pin 3      | I2C SDA   | GPIO 2  | BH1750 SDA                |
| Pin 4      | 5V        | –       | MQ135 VCC                 |
| Pin 5      | I2C SCL   | GPIO 3  | BH1750 SCL                |
| Pin 6      | GND       | –       | Rail trái (–)             |
| Pin 8      | UART TXD  | GPIO 14 | PMS7003 RX                |
| Pin 9      | GND       | –       | PMS7003 GND               |
| Pin 10     | UART RXD  | GPIO 15 | PMS7003 TX                |
| Pin 11     | GPIO      | GPIO 17 | DHT11 OUT                 |
| Pin 16     | GPIO      | GPIO 23 | Relay IN                  |
| Pin 20     | GND       | –       | MQ135 GND                 |
| Pin 30     | GND       | –       | Relay GND                 |
| Pin 34     | GND       | –       | Dây (–) của quạt          |
| Pin 37     | GPIO      | GPIO 26 | MQ135 DO (qua cầu chia áp) |
| Pin 39     | GND       | –       | Điện trở 20 kΩ của cầu chia áp |

## 3. Rail nguồn trên breadboard

| Rail               | Nối từ Raspberry Pi | Điện áp / vai trò |
| ------------------ | ------------------- | ----------------- |
| Rail trái, dấu (+) | Pin 1               | 3.3V              |
| Rail trái, dấu (–) | Pin 6               | GND (mass chung)  |
| Rail phải, dấu (+) | Pin 2               | 5V                |

Rail phải dấu (–) không được nối nên không dùng làm GND.

Rail trên breadboard có thể bị ngắt ở giữa. Trên mạch của dự án, BH1750 chỉ nhận được trên bus I2C sau khi chuyển dây nguồn của nó sang ngay cạnh dây nguồn của DHT11. Vì vậy:

- Dây nguồn của DHT11 và BH1750 cắm cùng một đoạn rail trái.
- Dây VCC của PMS7003, VCC và COM của relay cắm cùng một đoạn rail phải.
- MQ135, GND của relay, dây (–) của quạt và điện trở 20 kΩ nối thẳng vào chân GND của Pi, không qua rail.

## 4. Bảng nối từng thiết bị

### 4.1. DHT11 (nhiệt độ, độ ẩm)

| Chân DHT11 | Nối tới          | Loại kết nối   | Ghi chú            |
| ---------- | ---------------- | -------------- | ------------------ |
| + (VCC)    | Rail trái (+)    | Qua breadboard | 3.3V               |
| – (GND)    | Rail trái (–)    | Qua breadboard | GND                |
| OUT (DATA) | Pin 11 (GPIO 17) | Dây trực tiếp  | Tín hiệu số 1-wire |

### 4.2. BH1750 (cảm biến ánh sáng, I2C)

| Chân BH1750 | Nối tới        | Loại kết nối   | Ghi chú          |
| ----------- | -------------- | -------------- | ---------------- |
| VCC         | Rail trái (+)  | Qua breadboard | 3.3V             |
| GND         | Rail trái (–)  | Qua breadboard | GND              |
| SCL         | Pin 5 (GPIO 3) | Dây trực tiếp  | Bus I2C          |
| SDA         | Pin 3 (GPIO 2) | Dây trực tiếp  | Bus I2C          |
| ADD         | GND            | Qua breadboard | Địa chỉ I2C 0x23 |

### 4.3. PMS7003 (bụi mịn, UART)

| Chân PMS7003 | Nối tới               | Loại kết nối   | Ghi chú                       |
| ------------ | --------------------- | -------------- | ----------------------------- |
| VCC          | Rail phải (+)         | Qua breadboard | 5V                            |
| GND          | Pin 9                 | Dây trực tiếp  | GND                           |
| RX           | Pin 8 (GPIO 14, TXD)  | Dây trực tiếp  | Nối chéo: RX cảm biến ↔ TX Pi |
| TX           | Pin 10 (GPIO 15, RXD) | Dây trực tiếp  | Nối chéo: TX cảm biến ↔ RX Pi |

### 4.4. MQ135 (chất lượng không khí, chân digital DO)

Chỉ dùng chân DO, không dùng chân AO (để trống). Chân DO cho biết khí đã vượt ngưỡng hay chưa, ngưỡng chỉnh bằng biến trở trên module; không có giá trị nồng độ.

| Chân MQ135 | Nối tới                         | Loại kết nối   | Ghi chú                       |
| ---------- | ------------------------------- | -------------- | ----------------------------- |
| VCC        | Pin 4                           | Dây trực tiếp  | 5V, cho bộ sấy                |
| GND        | Pin 20                          | Dây trực tiếp  | GND                           |
| DO         | Điện trở 10 kΩ → hàng A         | Qua breadboard | Không nối thẳng vào GPIO      |
| AO         | Không nối                       | –              | Không dùng                    |

Cầu chia áp trên breadboard (dùng hai hàng trống, gọi là hàng A và hàng B):

| Điểm nối     | Nối tới          | Linh kiện / loại kết nối |
| ------------ | ---------------- | ------------------------ |
| DO của MQ135 | Hàng A           | Điện trở 10 kΩ           |
| Hàng A       | Pin 37 (GPIO 26) | Dây trực tiếp            |
| Hàng A       | Hàng B           | Điện trở 20 kΩ           |
| Hàng B       | Pin 39 (GND)     | Dây trực tiếp            |

Với mức cao 5V ở DO, điện áp tại GPIO khoảng 3.33V. Chỉ nối DO vào GPIO khi đèn nguồn của MQ135 đã sáng: mất GND thì DO bị kéo lên theo VCC, nên GPIO vẫn đọc 1 dù module không hoạt động.

### 4.5. Relay và quạt

Relay được điều khiển bằng GPIO 23: mức HIGH bật quạt, mức LOW tắt quạt.

| Chân relay | Nối tới           | Loại kết nối   | Ghi chú               |
| ---------- | ----------------- | -------------- | --------------------- |
| VCC (DC+)  | Rail phải (+)     | Qua breadboard | 5V, cùng đoạn rail với VCC của PMS7003 |
| GND (DC-)  | Pin 30            | Dây trực tiếp  | GND                   |
| IN         | Pin 16 (GPIO 23)  | Dây trực tiếp  | Tín hiệu điều khiển   |
| COM        | Rail phải (+)     | Qua breadboard | Nguồn 5V cho quạt, cùng đoạn rail với VCC của PMS7003 |
| NO         | Dây dương của quạt | Dây trực tiếp | Thường hở             |
| NC         | Không nối         | –              | Không dùng            |

| Dây quạt | Nối tới       | Loại kết nối   | Ghi chú              |
| -------- | ------------- | -------------- | -------------------- |
| Dây (+)  | NO của relay  | Dây trực tiếp  | Có điện khi relay đóng |
| Dây (–)  | Pin 34        | Dây trực tiếp  | GND                  |

Khi Pi vừa khởi động và relay nối đúng, GPIO 23 đọc mức thấp. Nếu GPIO 23 đọc mức cao, hãy kiểm tra dây GND của relay.

## 5. Địa chỉ I2C

| Thiết bị | Địa chỉ | Cấu hình    |
| -------- | ------- | ----------- |
| BH1750   | 0x23    | ADD nối GND |

Có thể kiểm tra bằng lệnh: `i2cdetect -y 1`

## 6. Lưu ý quan trọng

- Không nối thẳng DO của MQ135 vào GPIO: khi module cấp 5V, mức cao của DO có thể lên gần 5V, trong khi GPIO của Pi chỉ chịu được 3.3V. Luôn dùng cầu chia áp ở mục 4.4.
- MQ135 cần sấy vài phút sau khi cấp nguồn. Ngưỡng của DO chỉnh bằng biến trở trên module. Trên module của dự án, khi vặn biến trở: đèn DO sáng thì GPIO 26 đọc 0, đèn DO tắt thì GPIO 26 đọc 1. Tức là 0 = vượt ngưỡng, 1 = chưa vượt ngưỡng. Ngưỡng chưa được thử với khí thật.
- Không nối chân AO của MQ135 trực tiếp vào bất kỳ ngõ vào 3.3V nào, vì AO cũng có thể lên gần 5V.
- Quạt lấy nguồn từ 5V của Pi. Nếu `vcgencmd get_throttled` báo sụt áp khi quạt chạy, nên dùng nguồn 5V riêng và nối chung GND với Pi.
- PMS7003 dùng UART: cần bật Serial Port và tắt serial console trong raspi-config (Interface Options). Tín hiệu logic của PMS7003 là 3.3V nên nối trực tiếp với Pi được.
- I2C cần được bật trong raspi-config để dùng BH1750.
- Nếu DHT11 là chip rời (không có module), cần điện trở kéo lên 10kΩ giữa DATA và VCC.
- Tất cả GND (rail trái –, pin 9, 20, 30, 34, 39) đều là mass chung của Raspberry Pi; không nối rail 5V với rail 3.3V.
- Đầu dây cái cắm vào chân module có thể tiếp xúc kém. Nếu module mất nguồn dù nối đúng, hãy rút ra cắm lại cho chắc trước khi kết luận module hỏng.
- PMS7003 có đầu cắm 1.27mm nên thường cần board chuyển (adapter) để cắm vào breadboard.
- Tắt Pi và rút cáp nguồn trước khi thay đổi dây.

## 7. Trạng thái mạch

Kết quả lần kiểm tra cuối bằng `~/BTL_IOT/test_all.py` ngày 04/11/2026, với cách nối ở mục 4. Nếu đã đổi dây sau ngày này, hãy chạy lại script.

| Thiết bị     | Trạng thái     | Kết quả đo                                | Ghi chú                                                          |
| ------------ | -------------- | ----------------------------------------- | ---------------------------------------------------------------- |
| DHT11        | Đạt            | 28,5 °C, độ ẩm 83 %                       |                                                                  |
| PMS7003      | Đạt            | PM1.0: 26, PM2.5: 39, PM10: 47 µg/m³      |                                                                  |
| BH1750       | Đạt            | Địa chỉ 0x23, 170,8 lux                   | Dây nguồn cắm cùng đoạn rail trái với DHT11                      |
| MQ135 (DO)   | Đạt            | Đèn nguồn sáng, DO = 1 ở 20/20 lần đọc    | Đã xác nhận chiều tín hiệu bằng biến trở; chưa thử với khí thật  |
| Relay + quạt | Đạt            | GPIO 23 HIGH: quạt quay; LOW: quạt dừng   | Không báo sụt áp khi test (`throttled=0x0`)                      |
| ADS1115      | Đã bỏ khỏi mạch | Giữ SDA, SCL ở mức thấp, không đọc được  | Nhiều khả năng đã hỏng, chưa có module khác để đối chứng         |

Các giới hạn hiện tại:

- MQ135 chỉ cho biết đã vượt ngưỡng hay chưa, không có giá trị nồng độ. `test_all.py` báo lỗi khi DO = 0, vì không phân biệt được vượt ngưỡng với hở dây.
- `pi_sensor_client.py` (client cũ) chưa đọc MQ135 và PMS7003, chưa điều khiển quạt. CSV của nó chỉ gồm nhiệt độ, độ ẩm và ánh sáng.
- Dịch vụ `aiot/collector.py` (unit `deploy/aiot-collector.service`) đọc cả DHT11, BH1750, PMS7003 và MQ135 DO, ghi SQLite + `sensors.csv` + MQTT. Đã chép lên Pi và chạy thử 75 giây ở foreground ngày 07/11/2026: đọc đủ cảm biến, ghi CSV và SQLite, dừng sạch. **Chưa cài dịch vụ systemd** (README mục 2.4a). Collector và `pi_sensor_client.py` không được chạy cùng lúc vì dùng chung cảm biến. MQ135 được bỏ qua trong 180 giây đầu để sấy và không dùng để điều khiển.
- Bộ điều khiển quạt `aiot/controller.py` (relay GPIO23) có code và unit test, **chưa chạy trên mạch**. Lần đầu phải chạy demo với DB thử khi có người đứng cạnh quạt (README mục 2.4c).
- Bot Telegram, báo cáo sáng và lời khuyên Gemini chưa thử với bot và API thật; mô hình LSTM mới huấn luyện trên dữ liệu công khai, chưa chép lên Pi.
- `test.py` trên Pi vẫn yêu cầu ADS1115 nên sẽ báo lỗi; hãy dùng `test_all.py`.
- Phần quạt và đèn nguồn MQ135 của `test_all.py` cần người đứng cạnh mạch trả lời trong terminal (`ssh -t`).
