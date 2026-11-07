"""Test giải mã khung PMS7003 và đọc khung mới bằng serial giả (không cần pyserial, không sleep)."""
import struct

import pytest

from aiot.pms7003 import PmsReader, PmsReading, parse_frame


def make_frame(pm1, pm25, pm10, cf1=(1, 2, 3)):
    """Dựng khung 32 byte: 0x42 0x4D, độ dài 28, 13 giá trị uint16 big-endian, checksum.

    Giá trị CF=1 (byte 4–9) cố ý khác giá trị atmospheric (byte 10–15)
    để test bắt được nếu đọc nhầm vị trí.
    """
    data = list(cf1) + [pm1, pm25, pm10] + [0] * 7
    body = b"\x42\x4d" + struct.pack(">H", 28) + struct.pack(">13H", *data)
    return body + struct.pack(">H", sum(body))


class FakeClock:
    """Đồng hồ giả: mỗi lần serial giả đọc là thời gian trôi thêm một bước."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeSerial:
    """Serial giả: `buffered` là byte cũ đã nằm trong bộ đệm, `incoming` là byte đến sau.

    read() trả byte cũ trước; hết dữ liệu thì trả b"" như hết timeout của pyserial
    và đẩy đồng hồ giả lên đúng bằng timeout đang đặt. Nếu `endless` thì luôn
    trả rác để kiểm tra hạn chót tổng.
    """

    def __init__(self, clock, buffered=b"", incoming=b"", endless=None):
        self.clock = clock
        self.buffered = bytearray(buffered)
        self.incoming = bytearray(incoming)
        self.endless = endless
        self.timeout = 1.0
        self.reset_calls = 0

    def reset_input_buffer(self):
        self.reset_calls += 1
        self.buffered.clear()

    def read(self, size=1):
        src = self.buffered if self.buffered else self.incoming
        if src:
            chunk = bytes(src[:size])
            del src[:size]
            self.clock.now += 0.01
            return chunk
        if self.endless is not None:
            self.clock.now += 0.01
            return self.endless[:size]
        self.clock.now += self.timeout or 0.0
        return b""


def make_reader(**kwargs):
    clock = FakeClock()
    ser = FakeSerial(clock, **kwargs)
    return PmsReader(serial_obj=ser, clock=clock), ser, clock


def test_parse_reads_atmospheric_fields():
    reading = parse_frame(make_frame(12, 34, 56, cf1=(90, 91, 92)))
    assert reading == PmsReading(pm1=12, pm25=34, pm10=56)


def test_bad_checksum_raises():
    frame = bytearray(make_frame(1, 2, 3))
    frame[-1] ^= 0xFF
    with pytest.raises(ValueError):
        parse_frame(bytes(frame))


def test_bad_header_raises():
    frame = bytearray(make_frame(1, 2, 3))
    frame[0] = 0x41
    # Sửa checksum cho khớp để chắc chắn lỗi đến từ header
    frame[30:32] = struct.pack(">H", sum(frame[:30]))
    with pytest.raises(ValueError):
        parse_frame(bytes(frame))


def test_wrong_length_field_raises():
    frame = bytearray(make_frame(1, 2, 3))
    frame[2:4] = struct.pack(">H", 20)
    frame[30:32] = struct.pack(">H", sum(frame[:30]))
    with pytest.raises(ValueError):
        parse_frame(bytes(frame))


def test_short_frame_raises():
    with pytest.raises(ValueError):
        parse_frame(make_frame(1, 2, 3)[:31])


def test_read_fresh_resyncs_after_garbage():
    reader, _, _ = make_reader(incoming=b"\x00\x42" + make_frame(7, 8, 9))
    assert reader.read_fresh(timeout_s=3.0) == PmsReading(7, 8, 9)


def test_read_fresh_skips_corrupt_frame():
    bad = bytearray(make_frame(1, 1, 1))
    bad[-1] ^= 0xFF
    reader, _, _ = make_reader(incoming=bytes(bad) + make_frame(4, 5, 6))
    assert reader.read_fresh(timeout_s=3.0) == PmsReading(4, 5, 6)


def test_read_fresh_discards_buffered_frames():
    reader, ser, _ = make_reader(
        buffered=make_frame(5, 10, 15), incoming=make_frame(40, 80, 120)
    )
    assert reader.read_fresh(timeout_s=3.0).pm25 == 80
    assert ser.reset_calls == 1


def test_read_fresh_returns_none_on_timeout():
    reader, _, clock = make_reader()
    assert reader.read_fresh(timeout_s=3.0) is None
    assert clock.now <= 3.0 + 1e-9


def test_read_fresh_respects_deadline_with_endless_garbage():
    reader, _, clock = make_reader(endless=b"\x42\x00" * 32)
    assert reader.read_fresh(timeout_s=3.0) is None
    assert clock.now <= 3.0 + 0.02
