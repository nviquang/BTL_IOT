"""
Đọc cảm biến bụi PMS7003 qua UART (/dev/serial0, 9600 baud).

Khung 32 byte: 0x42 0x4D, độ dài 28 (uint16 big-endian), 13 giá trị uint16,
checksum là tổng 30 byte đầu. Lấy giá trị atmospheric ở byte 10–15
(giống test_all.py), không lấy giá trị CF=1 ở byte 4–9.

Cảm biến gửi khoảng 1 khung/giây. Vòng thu thập có thể bị chặn vài giây
(DHT11 thử lại), nên read_fresh() xoá bộ đệm trước rồi mới chờ khung mới,
tránh trả về số liệu cũ.
"""
import struct
import time
from typing import NamedTuple, Optional

HEADER = b"\x42\x4d"
FRAME_LEN = 32
LENGTH_FIELD = 28
BAUDRATE = 9600
DEFAULT_PORT = "/dev/serial0"
MAX_READ_WAIT_S = 0.5   # mỗi lần read() chờ tối đa chừng này, để kịp kiểm tra hạn chót


class PmsReading(NamedTuple):
    """Nồng độ bụi (µg/m³), giá trị atmospheric."""
    pm1: int
    pm25: int
    pm10: int


def parse_frame(frame: bytes) -> PmsReading:
    """Giải mã một khung 32 byte; khung sai thì raise ValueError."""
    if len(frame) != FRAME_LEN:
        raise ValueError(f"khung dài {len(frame)} byte, cần {FRAME_LEN}")
    if frame[:2] != HEADER:
        raise ValueError("sai header")
    length = struct.unpack_from(">H", frame, 2)[0]
    if length != LENGTH_FIELD:
        raise ValueError(f"trường độ dài {length}, cần {LENGTH_FIELD}")
    checksum = struct.unpack_from(">H", frame, 30)[0]
    if sum(frame[:30]) != checksum:
        raise ValueError("sai checksum")
    pm1, pm25, pm10 = struct.unpack_from(">3H", frame, 10)
    return PmsReading(pm1, pm25, pm10)


class PmsReader:
    """Đọc PMS7003. Có thể truyền serial_obj (serial giả khi test) thay cho cổng thật."""

    def __init__(self, port: str = DEFAULT_PORT, serial_obj=None, clock=time.monotonic):
        if serial_obj is None:
            import serial  # import muộn để laptop chạy test không cần pyserial

            serial_obj = serial.Serial(port, BAUDRATE, timeout=MAX_READ_WAIT_S)
        self._ser = serial_obj
        self._clock = clock

    def read_fresh(self, timeout_s: float = 3.0) -> Optional[PmsReading]:
        """Bỏ dữ liệu cũ trong bộ đệm, trả khung hợp lệ đầu tiên đến sau đó.

        Hết timeout_s mà chưa có khung hợp lệ (ví dụ rút dây PMS7003) thì trả None.
        Tổng thời gian chờ không vượt quá timeout_s đáng kể, kể cả khi rác đến liên tục.
        """
        self._ser.reset_input_buffer()
        deadline = self._clock() + timeout_s
        buf = bytearray()
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            self._ser.timeout = min(remaining, MAX_READ_WAIT_S)
            chunk = self._ser.read(FRAME_LEN)
            if not chunk:
                continue
            buf += chunk
            reading = self._extract(buf)
            if reading is not None:
                return reading

    @staticmethod
    def _extract(buf: bytearray) -> Optional[PmsReading]:
        """Tìm khung hợp lệ trong buf; bỏ byte rác phía trước (sửa buf tại chỗ)."""
        while True:
            start = buf.find(HEADER)
            if start < 0:
                # Giữ lại byte cuối nếu là 0x42, có thể là nửa đầu của header
                keep = 1 if buf.endswith(HEADER[:1]) else 0
                del buf[:len(buf) - keep]
                return None
            del buf[:start]
            if len(buf) < FRAME_LEN:
                return None
            try:
                return parse_frame(bytes(buf[:FRAME_LEN]))
            except ValueError:
                del buf[:1]  # header giả hoặc khung hỏng: dò tiếp từ byte sau
