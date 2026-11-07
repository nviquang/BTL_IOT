"""
Đọc phần cứng thật trên Raspberry Pi cho collector.

- DHT11: chân 11 (GPIO17), thử lại 3 lần cách 2,5 giây như pi_sensor_client.py.
- BH1750: I2C chân 3, 5, địa chỉ 0x23.
- PMS7003: UART /dev/serial0, đọc qua aiot.pms7003.PmsReader.
- MQ135: chân DO qua cầu chia áp vào pin 37 (GPIO26), lgpio chip 0, kéo xuống.
  0 = vượt ngưỡng, 1 = chưa vượt.

Thư viện phần cứng chỉ import khi mở thiết bị, để laptop chạy test không cần cài.
Mọi hàm read_* trả None khi lỗi, không raise. Thiết bị mở lỗi (thiếu cổng, console
đang chiếm UART, thiếu thư viện) thì thử mở lại sau REOPEN_S giây.
"""
import logging
import time

from aiot.pms7003 import BAUDRATE, DEFAULT_PORT, MAX_READ_WAIT_S, PmsReader, PmsReading

log = logging.getLogger(__name__)

DHT_RETRIES = 3                 # số lần thử đọc lại DHT11
DHT_RETRY_DELAY = 2.5           # giây nghỉ giữa 2 lần thử (DHT cần >= 2s)
BH1750_ADDRESS = 0x23           # 0x5C nếu kéo chân ADDR lên cao
GPIO_CHIP = 0
MQ135_DO_GPIO = 26              # pin 37
PMS_TIMEOUT_S = 3.0
REOPEN_S = 60                   # thử mở lại thiết bị lỗi sau chừng này giây


class PiSensors:
    """Gom các cảm biến của trạm. Mở thiết bị lười và tự thử mở lại khi lỗi."""

    def __init__(self, pms_port: str = DEFAULT_PORT, clock=time.monotonic, sleep=time.sleep):
        self._pms_port = pms_port
        self._clock = clock
        self._sleep = sleep
        self._dht = None
        self._light = None
        self._i2c = None
        self._pms = None
        self._pms_serial = None
        self._chip = None
        self._next_open = {"dht": 0.0, "lux": 0.0, "pms": 0.0, "mq135": 0.0}

    # ---------- mở thiết bị ----------
    def _may_open(self, name: str) -> bool:
        return self._clock() >= self._next_open[name]

    def _open_failed(self, key: str, label: str, err: Exception) -> None:
        log.warning("Không mở được %s: %s (thử lại sau %d s)", label, err, REOPEN_S)
        self._next_open[key] = self._clock() + REOPEN_S

    def _ensure_dht(self):
        if self._dht is None and self._may_open("dht"):
            try:
                import adafruit_dht
                import board

                # Dò pulseio GIỐNG HỆT logic của adafruit_dht để use_pulseio khớp
                # với đường đọc mà thư viện thực sự sẽ dùng.
                try:
                    from pulseio import PulseIn  # noqa: F401
                    has_pulseio = True
                except (ImportError, NotImplementedError):
                    has_pulseio = False
                if not has_pulseio:
                    log.warning("Không có pulseio (thiếu libgpiod2/sysv-ipc), DHT11 sẽ đọc lỗi nhiều")
                self._dht = adafruit_dht.DHT11(board.D17, use_pulseio=has_pulseio)
            except Exception as e:
                self._open_failed("dht", "DHT11", e)
        return self._dht

    def _ensure_light(self):
        if self._light is None and self._may_open("lux"):
            i2c = None
            try:
                import adafruit_bh1750
                import board
                import busio

                i2c = busio.I2C(board.SCL, board.SDA)
                self._light = adafruit_bh1750.BH1750(i2c, address=BH1750_ADDRESS)
                self._i2c = i2c
            except Exception as e:
                # Giải phóng bus vừa tạo, nếu không mỗi lần thử lại rò một file descriptor
                if i2c is not None:
                    _quiet(i2c.deinit)
                self._open_failed("lux", "BH1750", e)
        return self._light

    def _ensure_pms(self):
        if self._pms is None and self._may_open("pms"):
            try:
                import serial

                # Tự giữ cổng serial để close() đóng được
                self._pms_serial = serial.Serial(self._pms_port, BAUDRATE, timeout=MAX_READ_WAIT_S)
                self._pms = PmsReader(serial_obj=self._pms_serial)
            except Exception as e:
                self._pms_serial = None
                self._open_failed("pms", "PMS7003", e)
        return self._pms

    def _ensure_chip(self):
        if self._chip is None and self._may_open("mq135"):
            chip = None
            try:
                import lgpio

                chip = lgpio.gpiochip_open(GPIO_CHIP)
                lgpio.gpio_claim_input(chip, MQ135_DO_GPIO, lgpio.SET_PULL_DOWN)
                self._chip = chip
            except Exception as e:
                if chip is not None:
                    _quiet(lambda: lgpio.gpiochip_close(chip))
                self._open_failed("mq135", "MQ135 (lgpio)", e)
        return self._chip

    # ---------- đọc ----------
    def read_dht(self) -> tuple[float, float] | None:
        """Đọc DHT11, thử lại tối đa DHT_RETRIES lần. Trả về (temp, hum) hoặc None."""
        dht = self._ensure_dht()
        if dht is None:
            return None
        for attempt in range(1, DHT_RETRIES + 1):
            try:
                temp = dht.temperature      # lần đầu thực sự đo
                hum = dht.humidity          # trong 2s -> trả về đúng giá trị vừa đo
                if temp is not None and hum is not None:
                    return float(temp), float(hum)
                log.info("DHT11 lần %d: đọc được None, thử lại", attempt)
            except RuntimeError as e:
                # "Checksum did not validate", "A full buffer was not returned", ...
                log.info("DHT11 lần %d: %s", attempt, e)
            except Exception as e:
                # ví dụ OSError khi pulseio bị đứt -> không được làm chết vòng lặp
                log.warning("DHT11 lần %d: lỗi không mong đợi: %s", attempt, e)
            if attempt < DHT_RETRIES:
                self._sleep(DHT_RETRY_DELAY)
        return None

    def read_lux(self) -> float | None:
        """Đọc BH1750. Trả về lux hoặc None nếu lỗi I2C."""
        light = self._ensure_light()
        if light is None:
            return None
        try:
            return float(light.lux)      # .lux luôn là lần đo mới
        except Exception as e:
            log.warning("Lỗi đọc BH1750: %s", e)
            return None

    def read_pm(self) -> PmsReading | None:
        """Đọc một khung PMS7003 mới. Lỗi cổng thì đóng lại và mở lại sau REOPEN_S."""
        pms = self._ensure_pms()
        if pms is None:
            return None
        try:
            return pms.read_fresh(timeout_s=PMS_TIMEOUT_S)
        except Exception as e:
            self._close_pms()
            self._open_failed("pms", "PMS7003 (lỗi đọc)", e)
            return None

    def read_mq135(self) -> int | None:
        """Đọc bit DO của MQ135: 0 = vượt ngưỡng, 1 = chưa vượt, None nếu lỗi."""
        chip = self._ensure_chip()
        if chip is None:
            return None
        try:
            import lgpio

            return int(lgpio.gpio_read(chip, MQ135_DO_GPIO))
        except Exception as e:
            self._close_chip()
            self._open_failed("mq135", "MQ135 (lỗi đọc)", e)
            return None

    # ---------- dọn dẹp ----------
    def _close_pms(self):
        if self._pms_serial is not None:
            _quiet(self._pms_serial.close)
        self._pms = None
        self._pms_serial = None

    def _close_chip(self):
        if self._chip is not None:
            chip = self._chip

            def close():
                import lgpio
                lgpio.gpiochip_close(chip)

            _quiet(close)
        self._chip = None

    def close(self) -> None:
        """Dọn tiến trình libgpiod_pulsein của DHT, giải phóng bus I2C, đóng UART và chip GPIO."""
        if self._dht is not None:
            _quiet(self._dht.exit)
            self._dht = None
        self._light = None
        if self._i2c is not None:
            _quiet(self._i2c.deinit)
            self._i2c = None
        self._close_pms()
        self._close_chip()


def _quiet(fn):
    try:
        fn()
    except Exception:
        pass
