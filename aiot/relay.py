"""
Relay quạt lọc trên GPIO23 (pin 16), qua lgpio.

- Relay kích mức HIGH: HIGH = quạt quay, LOW = quạt dừng.
- Chỉ dịch vụ controller được giữ chân này; bot chỉ ghi khoá pending_override.
- Nhận chân với mức ban đầu lấy từ trạng thái đã khôi phục, để khởi động lại khi
  đang BẢO VỆ không làm quạt tắt một nhịp.
- close() đưa chân về LOW rồi nhả chip: dịch vụ dừng thì không ai giám sát quạt nữa.

lgpio được import trong __init__ để laptop chạy test không cần thư viện này
(lỗi cài đặt trên Python 3.13: xem README Phụ lục C).
"""
GPIO_CHIP = 0          # gpiochip của header 40 chân trên Pi 4
RELAY_GPIO = 23


class Relay:
    def __init__(self, pin: int = RELAY_GPIO, initial: bool = False, chip: int = GPIO_CHIP):
        import lgpio

        self._lgpio = lgpio
        self.pin = pin
        self._handle = lgpio.gpiochip_open(chip)
        try:
            lgpio.gpio_claim_output(self._handle, pin, 1 if initial else 0)
        except Exception:
            lgpio.gpiochip_close(self._handle)
            self._handle = None
            raise

    def set(self, on: bool) -> None:
        """Bật/tắt quạt. Gọi lặp lại với cùng giá trị không sao (ghi lại cùng mức)."""
        if self._handle is None:
            raise RuntimeError("Relay đã đóng")
        self._lgpio.gpio_write(self._handle, self.pin, 1 if on else 0)

    def close(self) -> None:
        """Tắt quạt (LOW) và nhả chip; gọi nhiều lần không lỗi."""
        if self._handle is None:
            return
        try:
            self._lgpio.gpio_write(self._handle, self.pin, 0)
        finally:
            self._lgpio.gpiochip_close(self._handle)
            self._handle = None
