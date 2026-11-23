#!/usr/bin/env python3
"""
Test tất cả module: DHT11, PMS7003, BH1750, quạt qua relay, còi báo.
Chạy: cd ~/BTL_IOT && source venv/bin/activate && python test_all.py
Mã thoát: 0 nếu tất cả OK, 1 nếu có mục lỗi hoặc bị bỏ qua.
Phần quạt và còi sẽ bật/tắt thiết bị và hỏi bạn quạt có quay / còi có kêu không, nên phải
chạy ở terminal cạnh mạch. Không có terminal (chạy nền, cron) thì bỏ qua, không đụng tới chân.

Khác test.py: kiểm tra bus I2C trước khi quét. SDA bị kéo thấp sẽ làm mọi địa chỉ
"trả lời" giả và đọc ra toàn số 0, nên phải loại trường hợp này để không báo OK sai.
"""
import signal
import subprocess
import sys
import time

# ====== CHỈNH THEO MẠCH CỦA BẠN ======
DHT_GPIO = "D17"             # DHT11 nối pin 11 = GPIO17
PMS_PORT = "/dev/serial0"    # PMS7003 nối UART (TX cảm biến -> RX Pi, GPIO15)
I2C_BUS = 1
BH1750_ADDRS = (0x23, 0x5C)
RELAY_GPIO = 23              # relay IN nối pin 16 = GPIO23
BUZZER_GPIO = 5              # còi I/O nối pin 29 = GPIO5 (VCC pin 17 = 3.3V, GND pin 25)
GPIO_CHIP = 0                # gpiochip của header 40 chân trên Pi 4
# =====================================

OK, FAIL, SKIP = "✔ OK", "✘ LỖI", "- BỎ QUA"
MAX_REAL_DEVICES = 10        # thấy nhiều hơn số này là phản hồi giả

results = {}


class TestTimeout(Exception):
    pass


def on_alarm(signum, frame):
    raise TestTimeout("quá thời gian chờ")


signal.signal(signal.SIGALRM, on_alarm)


def report(name, status, detail):
    results[name] = (status, detail)
    print(f"{status:8s} {name}: {detail}")


def header(name):
    print(f"\n--- {name} ---")


def read_i2c_levels():
    """Đọc mức SDA, SCL 5 lần. Trả về danh sách (sda, scl)."""
    samples = []
    for _ in range(5):
        out = subprocess.run(["pinctrl", "lev", "2,3"], capture_output=True, text=True, timeout=5).stdout
        sda, scl = out.split()[:2]
        samples.append((int(sda), int(scl)))
        time.sleep(0.2)
    return samples


def test_dht():
    header("DHT11")
    import board, adafruit_dht
    dht = None
    signal.alarm(30)
    try:
        # use_pulseio phải bật, nếu không DHT11 báo "sensor not found" dù dây đúng
        dht = adafruit_dht.DHT11(getattr(board, DHT_GPIO), use_pulseio=True)
        last_err = "không có dữ liệu"
        for _ in range(6):   # DHT11 hay đọc lỗi, thử lại vài lần
            try:
                t, h = dht.temperature, dht.humidity
                if t is not None and h is not None:
                    report("DHT11", OK, f"{t} °C, {h} %")
                    return
            except RuntimeError as e:
                last_err = e
            time.sleep(2.5)
        report("DHT11", FAIL, f"đọc thất bại sau 6 lần ({last_err}). Kiểm tra DATA, nguồn")
    except Exception as e:
        report("DHT11", FAIL, str(e))
    finally:
        signal.alarm(0)
        if dht:
            dht.exit()


def test_pms():
    header("PMS7003")
    import serial
    signal.alarm(15)
    try:
        with serial.Serial(PMS_PORT, 9600, timeout=1) as ser:
            ser.reset_input_buffer()
            buf = b""
            deadline = time.time() + 6
            while time.time() < deadline:
                buf += ser.read(64)
        if not buf:
            report("PMS7003", FAIL, "không nhận được byte nào. Kiểm tra 5V, quạt có quay không, TX -> pin 10")
            return
        start = buf.find(b"\x42\x4d")
        while start >= 0 and start + 32 <= len(buf):
            frame = buf[start:start + 32]
            if sum(frame[:30]) == int.from_bytes(frame[30:32], "big"):
                pm1 = int.from_bytes(frame[10:12], "big")
                pm25 = int.from_bytes(frame[12:14], "big")
                pm10 = int.from_bytes(frame[14:16], "big")
                report("PMS7003", OK, f"PM1.0: {pm1}, PM2.5: {pm25}, PM10: {pm10} µg/m³")
                return
            start = buf.find(b"\x42\x4d", start + 1)
        report("PMS7003", FAIL, f"có {len(buf)} byte nhưng không có frame hợp lệ. Kiểm tra tiếp xúc, GND chung")
    except Exception as e:
        report("PMS7003", FAIL, str(e))
    finally:
        signal.alarm(0)


def scan_i2c(bus):
    """Trả về danh sách địa chỉ trả lời, hoặc None nếu bus timeout."""
    found = []
    for addr in range(0x03, 0x78):
        try:
            bus.read_byte(addr)
            found.append(addr)
        except TimeoutError:
            return None
        except OSError:
            pass
    return found


def test_bh1750(bus, found):
    header("BH1750")
    addr = next((a for a in BH1750_ADDRS if a in found), None)
    if addr is None:
        report("BH1750", FAIL, "không thấy 0x23/0x5C. Kiểm tra VCC 3.3V, GND, SDA, SCL")
        return
    try:
        bus.write_byte(addr, 0x10)   # continuous high-res mode
        time.sleep(0.2)
        d = bus.read_i2c_block_data(addr, 0x10, 2)
        lux = ((d[0] << 8) | d[1]) / 1.2
        report("BH1750", OK, f"địa chỉ {hex(addr)}, {lux:.1f} lux")
    except OSError as e:
        report("BH1750", FAIL, str(e))


def test_i2c():
    names = ("BH1750",)
    header("Bus I2C")

    def skip_all(reason):
        for name in names:
            report(name, SKIP, reason)

    try:
        levels = read_i2c_levels()
    except Exception as e:
        skip_all(f"không đọc được mức chân ({e})")
        return
    print("Mức SDA SCL:", " | ".join(f"{a} {b}" for a, b in levels))
    if any(lv != (1, 1) for lv in levels):
        print("LỖI - bus phải luôn là 1 1 khi rảnh. Mức thấp nghĩa là có module mất nguồn, chập GND hoặc dây sai.")
        print("      Không quét để tránh báo OK giả. Tắt Pi và kiểm tra dây rồi chạy lại.")
        skip_all("bus I2C không rảnh")
        return

    from smbus2 import SMBus
    signal.alarm(30)
    try:
        with SMBus(I2C_BUS) as bus:
            found = scan_i2c(bus)
            if found is None:
                print("LỖI - quét bị timeout, có thể SCL đang bị kéo thấp")
                skip_all("bus I2C bị timeout")
                return
            print("Địa chỉ tìm thấy:", [hex(a) for a in found] or "không có")
            if len(found) > MAX_REAL_DEVICES:
                print("LỖI - quá nhiều địa chỉ trả lời, đây là phản hồi giả")
                skip_all("phản hồi I2C giả")
                return
            test_bh1750(bus, found)
    except TestTimeout as e:
        for name in names:
            if name not in results:
                report(name, FAIL, str(e))
    finally:
        signal.alarm(0)


def read_throttled():
    """Trả về giá trị vcgencmd get_throttled, hoặc None nếu không đọc được."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip().split("=")[1], 16)
    except Exception:
        return None


def ask_fan_spinning(level):
    while True:
        answer = input(f"  Relay IN = {'HIGH' if level else 'LOW'}. Quạt có quay không? (c/k): ").strip().lower()
        if answer in ("c", "k"):
            return answer == "c"


def test_fan():
    header("Quạt (relay)")
    if not sys.stdin.isatty():
        report("Quạt", SKIP, "cần chạy ở terminal cạnh mạch để xác nhận quạt quay")
        return
    input("Quạt sẽ được bật/tắt. Giữ tay và dây xa cánh quạt rồi nhấn Enter...")

    import lgpio
    chip = None
    spinning = {}
    throttled_before = read_throttled()
    try:
        chip = lgpio.gpiochip_open(GPIO_CHIP)
        lgpio.gpio_claim_output(chip, RELAY_GPIO, 1)
        for level in (1, 0):
            lgpio.gpio_write(chip, RELAY_GPIO, level)
            time.sleep(1)    # đợi relay đóng ngắt và quạt kịp quay
            spinning[level] = ask_fan_spinning(level)

        # Trả relay về mức tắt trước khi nhả chân, nếu có mức tắt
        if not spinning[1]:
            lgpio.gpio_write(chip, RELAY_GPIO, 1)
        elif not spinning[0]:
            lgpio.gpio_write(chip, RELAY_GPIO, 0)
    except Exception as e:
        report("Quạt", FAIL, str(e))
        return
    finally:
        if chip is not None:
            lgpio.gpiochip_close(chip)

    throttled_after = read_throttled()
    power_warning = ""
    if throttled_before is not None and throttled_after is not None:
        if throttled_after & 0x10001 and throttled_after != throttled_before:
            power_warning = f". Cảnh báo: có sụt áp khi test (throttled={hex(throttled_after)}), nên dùng nguồn 5V riêng"

    if spinning[1] and spinning[0]:
        report("Quạt", FAIL, "quạt quay ở cả 2 mức: relay không ngắt với tín hiệu 3.3V, hoặc dây đang nối NC hay bỏ qua relay" + power_warning)
    elif not spinning[1] and not spinning[0]:
        report("Quạt", FAIL, "quạt không quay ở cả 2 mức: kiểm tra 5V tới COM, NO, dây quạt, GND và relay VCC" + power_warning)
    elif spinning[1]:
        report("Quạt", OK, "relay kích mức HIGH (tắt khi IN = LOW)" + power_warning)
    else:
        report("Quạt", OK, "relay kích mức LOW. Lưu ý: GPIO23 mặc định LOW nên quạt có thể tự chạy khi Pi khởi động hoặc sau khi script thoát" + power_warning)


def ask_buzzer_sounding(level):
    while True:
        answer = input(f"  Còi I/O = {'HIGH' if level else 'LOW'}. Còi có kêu không? (c/k): ").strip().lower()
        if answer in ("c", "k"):
            return answer == "c"


def test_buzzer():
    header("Còi báo")
    if not sys.stdin.isatty():
        report("Còi", SKIP, "cần chạy ở terminal cạnh mạch để xác nhận còi kêu")
        return
    input("Còi sẽ kêu vài giây. Nhấn Enter để bắt đầu...")

    import lgpio
    chip = None
    sounding = {}
    try:
        chip = lgpio.gpiochip_open(GPIO_CHIP)
        lgpio.gpio_claim_output(chip, BUZZER_GPIO, 1)
        for level in (0, 1):
            lgpio.gpio_write(chip, BUZZER_GPIO, level)
            time.sleep(0.5)
            sounding[level] = ask_buzzer_sounding(level)
            lgpio.gpio_write(chip, BUZZER_GPIO, 1 if sounding[0] else 0)   # tắt trong lúc chờ
        # Trả về mức im trước khi nhả chân
        lgpio.gpio_write(chip, BUZZER_GPIO, 1 if sounding[0] else 0)
    except Exception as e:
        report("Còi", FAIL, str(e))
        return
    finally:
        if chip is not None:
            lgpio.gpiochip_close(chip)

    if sounding[0] and sounding[1]:
        report("Còi", FAIL, "còi kêu ở cả 2 mức: VCC đang cấp 5V (phải dùng 3.3V, pin 17) hoặc I/O chưa nối GPIO5")
    elif not sounding[0] and not sounding[1]:
        report("Còi", FAIL, "còi không kêu ở cả 2 mức: kiểm tra VCC pin 17, GND pin 25, I/O pin 29")
    elif sounding[0]:
        report("Còi", OK, "còi kích mức thấp (đúng với buzzer.active_low: true)")
    else:
        report("Còi", OK, "còi kích mức cao: đặt buzzer.active_low: false trong config.yaml")


SENSOR_SERVICES = ("aiot-collector", "aiot-controller", "pi-sensor")


def busy_services():
    """Dịch vụ đang giữ cảm biến/relay/còi; chạy cùng lúc thì test không nhận được chân."""
    busy = []
    for name in SENSOR_SERVICES:
        try:
            out = subprocess.run(["systemctl", "is-active", name], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
        except Exception:
            continue
        if out == "active":
            busy.append(name)
    return busy


def main():
    busy = busy_services()
    if busy:
        print("Các dịch vụ đang giữ cảm biến, relay và còi: " + ", ".join(busy))
        print("Dừng trước:  bash scripts/pi_up.sh --stop")
        print("Test xong bật lại:  bash scripts/pi_up.sh")
        return 1
    test_dht()
    test_pms()
    test_i2c()
    test_fan()
    test_buzzer()

    print("\n========== TỔNG KẾT ==========")
    for name, (status, detail) in results.items():
        print(f"{name:10s}: {status}")
    return 0 if all(s == OK for s, _ in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
