"""
Còi báo (module còi chủ động 3 chân GND, I/O, VCC) trên GPIO5 (pin 29), qua lgpio.

- Module đang dùng ghi 低电平触发 = kích mức thấp: I/O = LOW thì kêu, HIGH thì im.
  Cấp VCC từ 3.3V (pin 17), không dùng 5V: module dùng transistor PNP, cấp 5V thì
  mức HIGH 3.3V của Pi không tắt được transistor và còi kêu mãi.
- GPIO5 mặc định kéo lên khi Pi khởi động, nên còi im cho tới khi controller nhận chân.
- burst() kêu ngắt quãng trong luồng nền để không chặn vòng điều khiển; silence() tắt ngay.
- close() tắt còi rồi nhả chip; gọi nhiều lần không lỗi.

lgpio được import trong __init__ để laptop chạy test không cần thư viện này.
"""
import threading
import time

GPIO_CHIP = 0
BUZZER_GPIO = 5


class Buzzer:
    def __init__(self, pin: int = BUZZER_GPIO, active_low: bool = True, chip: int = GPIO_CHIP,
                 gpio=None):
        if gpio is None:
            import lgpio as gpio
        self._gpio = gpio
        self.pin = pin
        self.active_low = active_low
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._handle = gpio.gpiochip_open(chip)
        try:
            gpio.gpio_claim_output(self._handle, pin, self._level(False))
        except Exception:
            gpio.gpiochip_close(self._handle)
            self._handle = None
            raise

    def _level(self, on: bool) -> int:
        return int(on) ^ int(self.active_low)

    def _write(self, on: bool) -> None:
        with self._lock:
            if self._handle is None:
                raise RuntimeError("Còi đã đóng")
            self._gpio.gpio_write(self._handle, self.pin, self._level(on))

    def set(self, on: bool) -> None:
        """Bật/tắt còi liên tục (dùng cho test phần cứng)."""
        self._write(on)

    def burst(self, on_s: float, off_s: float, duration_s: float) -> None:
        """Kêu ngắt quãng (on_s kêu, off_s im) trong duration_s giây, không chặn."""
        self.silence()
        self._stop = threading.Event()
        stop = self._stop

        def loop():
            end = time.monotonic() + duration_s
            try:
                while not stop.is_set() and time.monotonic() < end:
                    self._write(True)
                    if stop.wait(on_s):
                        break
                    self._write(False)
                    stop.wait(off_s)
            except Exception:
                pass                    # còi lỗi không được làm hỏng controller
            finally:
                try:
                    self._write(False)
                except Exception:
                    pass

        self._thread = threading.Thread(target=loop, name="buzzer", daemon=True)
        self._thread.start()

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def silence(self) -> None:
        """Dừng hồi còi đang kêu (nếu có) và đưa chân về mức im."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._handle is not None:
            self._write(False)

    def close(self) -> None:
        if self._handle is None:
            return
        try:
            self.silence()
        finally:
            with self._lock:
                self._gpio.gpiochip_close(self._handle)
                self._handle = None
