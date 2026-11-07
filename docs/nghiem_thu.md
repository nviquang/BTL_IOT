# Biên bản nghiệm thu trạm không khí AIoT

Bảng kiểm thử lấy từ mục "Kiểm thử mục tiêu" trong `CLAUDE.md`. Các phép thử dài ngày do nhóm
tự làm trên Pi; cột "Kết quả" chỉ được điền bằng số đo thật (kèm ngày đo và người đo).
**Chưa có phép thử nào được thực hiện** tính tới 07/11/2026.

Điều kiện trước khi đo: đã làm xong các bước trong mục "Việc cần làm trên Pi" của `README.md`
(collector, controller, bot, hai timer đều đang chạy), đồng hồ Pi đã đồng bộ NTP.

Lệnh tính chỉ số (chạy trên Pi, giờ địa phương; mặc định 7 ngày trước `--end`):

```bash
cd ~/BTL_IOT && venv/bin/python scripts/acceptance_report.py \
    --start "2026-11-10" --end "2026-11-17" [--public-csv tram.csv] [--tolerance-min 15]
```

Script chỉ đọc DB (`db_path` trong `config.yaml`, đổi bằng `--db`), in từng nhóm chỉ số. Định
nghĩa chi tiết của từng chỉ số nằm ở docstring đầu file `scripts/acceptance_report.py`.

| Nhóm | Cách làm | Tiêu chí | Cách đo | Kết quả |
| ---- | -------- | -------- | ------- | ------- |
| Cảm biến | So với trạm công khai ≥ 7 ngày | Tương quan cao sau hiệu chỉnh tuyến tính; ghi kèm độ ẩm | Lấy PM2.5 theo giờ của một trạm đo công khai gần nhất trong cùng khoảng, lưu CSV cột `timestamp` (giờ địa phương), `pm25`; chạy script với `--public-csv`. Mục `[Cảm biến]` in số giờ chung, `r`, hệ số hiệu chỉnh `công khai ≈ a · trạm + b`, MAE trước/sau, độ ẩm TB và cao nhất | chưa đo |
| Ổn định | Chạy liên tục ≥ 7 ngày | Hợp lệ ≥ 95%, không treo | Mục `[Ổn định]`: tỉ lệ cửa sổ 5 phút `valid=1` so với số cửa sổ **kỳ vọng** (Pi tắt cũng tính là thiếu). Không treo: `systemctl status aiot-collector aiot-controller aiot-bot` không có lần khởi động lại ngoài ý muốn (`journalctl -u aiot-collector --since ...`) | chưa đo |
| Dự báo | Từ Task 12 | LSTM vượt persistence ở horizon ≥ 2 giờ | `training/02_danh_gia.ipynb` (README 2.4g). Khi có đủ dữ liệu của trạm: chép `aiot.db` về laptop, `python training/export_hourly.py --db aiot.db --out hourly.csv`, rồi đánh giá lại trên chuỗi này | chưa đo trên dữ liệu của trạm (xem ghi chú 1) |
| Điều khiển | Hộp khói, 10 lần vượt ngưỡng, có người trông | Đúng thời gian cấu hình, không nhảy trạng thái | Controller chạy với `config.yaml` thật (`demo: false`). Ghi giờ từng lần thử. Mục `[Điều khiển]`: thời gian phản ứng mỗi lần bật (giây, từ mẫu thô đầu tiên của chuỗi PM2.5 > `enter_pm25` liên tục tới sự kiện `fan_on`), so với giới hạn `enter_mean_s + enter_hold_s + loop_s` (mặc định 920 s; mức ≥ 150: `danger_window_s + loop_s`), và số lần nhảy trạng thái sai luật (phải bằng 0): BẢO VỆ giữ chưa tới `min_state_s` (fan_on → fan_off sớm hơn 600 s), hoặc vào lại vì vượt ngưỡng (`enter`) chưa tới `enter_hold_s` (300 s) sau khi thoát. Số lần vào lại nhanh (< `min_state_s` sau khi thoát) được in riêng để tham khảo, vì luật cho phép vào lại sau `enter_hold_s` hoặc ngay khi nguy hại/dự báo cao. Chạy trong 7 ngày giữ mẫu thô | chưa đo |
| Hiệu quả lọc | So đường PM2.5 khi quạt bật và tắt | Có khác biệt đo được | Mục `[Hiệu quả lọc]`: số cửa sổ, PM2.5 TB và độ dốc (µg/m³/giờ) khi quạt bật cả cửa sổ và khi tắt. So độ dốc, không chỉ so trung bình (quạt bật đúng lúc không khí xấu nên trung bình lúc bật thường cao hơn). Nên làm thêm một lần có kiểm soát: cùng lượng khói, một lần ghi đè `/fan on`, một lần `/fan off` | chưa đo |
| Telegram | Báo cáo sáng 7 ngày, có một lần tắt mạng | Đúng giờ ≥ 6/7, có gửi bù | Mục `[Telegram]`: từng ngày giờ tạo và giờ gửi tin `morning_report`, số ngày đúng giờ (gửi trong 15 phút sau 06:30, đổi bằng `--tolerance-min`). Một ngày tắt Wi-Fi/router trước 06:30, bật lại sau 07:00: ngày đó phải hiện "KHÔNG đúng giờ" nhưng có giờ gửi (đã gửi bù) | chưa đo |
| Gemini | Giả lập lỗi API | Dùng mẫu cố định, bản tin vẫn gửi | README 2.4f, "Kiểm tra thật" bước 2: đặt `GEMINI_API_KEY` sai, `sudo systemctl restart aiot-bot`, gửi `/report`. Phải có báo cáo kèm "(lời khuyên mẫu)", log có "Gọi Gemini lỗi" và không in key | chưa đo |
| An toàn | Dừng `aiot-collector` khi đang chạy; rồi tắt Pi, rút dây PMS7003, bật lại | Cảnh báo lỗi, không bật/tắt bừa | (1) `sudo systemctl stop aiot-collector`, chờ > 2 phút: có sự kiện `sensor_fault` và tin cảnh báo, trạng thái quạt giữ nguyên: `sqlite3 ~/iot_data/aiot.db "select datetime(ts,'unixepoch','localtime'), kind, detail from events order by ts desc limit 10;"`. (2) `sudo shutdown -h now`, rút cáp nguồn, tháo dây PMS7003, bật lại: tương tự, không có `fan_on`/`fan_off` mới. Tắt nguồn lại trước khi cắm dây PMS7003 | chưa đo |

Ghi chú:

1. Đánh giá sơ bộ trên dữ liệu công khai CAMS của Hà Nội (không phải số đo của trạm), ngày
   07/11/2026: LSTM vượt persistence ở mọi horizon 1–6 giờ (MAE test giảm 21,7–31,1%), gần
   bằng hồi quy Ridge trên đặc trưng trễ. Chi tiết trong `training/02_danh_gia.ipynb`. Kết quả
   này chưa chứng minh mô hình đúng với cảm biến trong nhà.
2. CLAUDE.md ghi phép thử an toàn là "Rút dây PMS7003", nhưng dự án cấm tháo dây khi Pi đang
   có điện. Vì vậy mất dữ liệu khi đang chạy được giả lập bằng cách dừng `aiot-collector`;
   mất dây thật thì tháo khi đã tắt nguồn.
3. Thử khói chỉ dùng lượng nhỏ, nơi thoáng, có người trông. Quạt mini là mô phỏng: báo cáo
   phải ghi rõ đây là proof of concept.
4. Độ ẩm cao có thể làm PMS7003 đọc cao hơn thực tế; khi so với trạm công khai, ghi độ ẩm
   cùng lúc (script in độ ẩm TB và cao nhất).
5. Collector chỉ giữ mẫu thô 7 ngày; chạy script trong vòng 7 ngày sau phép thử điều khiển,
   nếu không thời gian phản ứng không còn tính được (các chỉ số khác dùng bảng `readings`,
   `events`, `outbox` giữ lâu dài).
