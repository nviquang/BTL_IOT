"""
Đọc phần cứng thật trên Raspberry Pi cho collector.

- DHT11: chân 11 (GPIO17), thử lại 3 lần cách 2,5 giây.
- BH1750: I2C chân 3, 5, địa chỉ 0x23.
- PMS7003: UART /dev/serial0, đọc qua aiot.pms7003.PmsReader.

Thư viện phần cứng chỉ import khi mở thiết bị, để laptop chạy test không cần cài.
Mọi hàm read_* trả None khi lỗi, không raise. Thiết bị mở lỗi (thiếu cổng, console
đang chiếm UART, thiếu thư viện) thì thử mở lại sau REOPEN_S giây.
"""
import logging
import threading
import time

from aiot.pms7003 import BAUDRATE, DEFAULT_PORT, MAX_READ_WAIT_S, PmsReader, PmsReading

log = logging.getLogger(__name__)

DHT_RETRIES = 3                 # số lần thử đọc lại DHT11
DHT_RETRY_DELAY = 2.5           # giây nghỉ giữa 2 lần thử (DHT cần >= 2s)
BH1750_ADDRESS = 0x23           # 0x5C nếu kéo chân ADDR lên cao
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
        self._next_open = {"dht": 0.0, "lux": 0.0, "pms": 0.0}

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

    # ---------- dọn dẹp ----------
    def _close_pms(self):
        if self._pms_serial is not None:
            _quiet(self._pms_serial.close)
        self._pms = None
        self._pms_serial = None

    def close(self) -> None:
        """Dọn tiến trình libgpiod_pulsein của DHT, giải phóng bus I2C và đóng UART."""
        if self._dht is not None:
            _quiet(self._dht.exit)
            self._dht = None
        self._light = None
        if self._i2c is not None:
            _quiet(self._i2c.deinit)
            self._i2c = None
        self._close_pms()


def _quiet(fn):
    try:
        fn()
    except Exception:
        pass


class SensorHub:
    """Đọc cảm biến ở nhịp nhanh cho bộ nhận diện nguồn bụi (mẫu 1 giây).

    Mỗi cảm biến chạy trong một luồng riêng và giữ giá trị mới nhất kèm thời điểm đọc:
    PMS7003 ~1 khung/giây, BH1750 mỗi giây, DHT11 mỗi ~2 giây (thử lại chặn tới 5 giây,
    không được làm trễ PM). snapshot() trả giá trị còn mới (<= max_age), cũ hơn thì None.

    Cũng có read_dht/read_lux/read_pm trả giá trị mới nhất, để collect_once (mẫu 10 giây)
    dùng chung mà không đọc phần cứng lần thứ hai.
    """

    MAX_AGE_S = {"pm": 3.0, "lux": 3.0, "dht": 10.0}
    PERIOD_S = {"pm": 0.0, "lux": 1.0, "dht": 2.0}

    def __init__(self, sensors, clock=time.monotonic):
        self._sensors = sensors
        self._clock = clock
        self._lock = threading.Lock()
        self._latest = {"pm": (None, None), "lux": (None, None), "dht": (None, None)}
        self._stop = threading.Event()
        self._threads = []

    def _reader(self, key):
        return {"pm": self._sensors.read_pm, "lux": self._sensors.read_lux,
                "dht": self._sensors.read_dht}[key]

    def poll(self, key) -> None:
        """Đọc một lần cảm biến key và lưu kết quả (luồng gọi lặp lại; test gọi trực tiếp)."""
        try:
            value = self._reader(key)()
        except Exception as e:
            log.warning("Lỗi đọc %s: %s", key, e)
            value = None
        if value is not None:
            with self._lock:
                self._latest[key] = (value, self._clock())

    def _loop(self, key):
        period = self.PERIOD_S[key]
        while not self._stop.is_set():
            start = self._clock()
            self.poll(key)
            wait = period - (self._clock() - start)
            if wait > 0:
                self._stop.wait(wait)
            elif key == "pm" and self._latest["pm"][0] is None:
                self._stop.wait(0.5)        # PMS chưa mở được: tránh vòng lặp bận

    def start(self) -> None:
        for key in ("pm", "lux", "dht"):
            t = threading.Thread(target=self._loop, args=(key,), name=f"sensor-{key}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=6)

    def _fresh(self, key):
        with self._lock:
            value, at = self._latest[key]
        if value is None or at is None or self._clock() - at > self.MAX_AGE_S[key]:
            return None
        return value

    def read_pm(self):
        return self._fresh("pm")

    def read_lux(self):
        return self._fresh("lux")

    def read_dht(self):
        return self._fresh("dht")

    def snapshot(self) -> dict:
        """{cột fast_readings: giá trị hoặc None} từ giá trị còn mới của mỗi cảm biến."""
        pm, lux, dht = self.read_pm(), self.read_lux(), self.read_dht()
        out = {c: None for c in ("pm1", "pm25", "pm10", "n03", "n05", "n10", "n25", "n50",
                                 "n100", "temp", "hum", "lux")}
        if pm is not None:
            for name in ("pm1", "pm25", "pm10", "n03", "n05", "n10", "n25", "n50", "n100"):
                v = getattr(pm, name, None)
                out[name] = float(v) if v is not None else None
        if lux is not None:
            out["lux"] = float(lux)
        if dht is not None:
            out["temp"], out["hum"] = float(dht[0]), float(dht[1])
        return out
