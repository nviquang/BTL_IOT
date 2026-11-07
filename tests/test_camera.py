"""Test chụp ảnh camera USB (cv2 giả, không cần camera thật) và xoá ảnh cũ."""
import os
import subprocess
import sys
import time
import types

import pytest

from aiot import camera


class FakeBuffer:
    def __init__(self, data):
        self._data = data

    def tobytes(self):
        return self._data


class FakeCapture:
    def __init__(self, opened=True, frames_ok=True, raise_on_read=False):
        self.opened = opened
        self.frames_ok = frames_ok
        self.raise_on_read = raise_on_read
        self.reads = 0
        self.released = False
        self.props = {}

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.props[prop] = value
        return True

    def read(self):
        if self.raise_on_read:
            raise RuntimeError("camera hỏng")
        self.reads += 1
        return (True, ["frame", self.reads]) if self.frames_ok else (False, None)

    def release(self):
        self.released = True


def make_cv2(cap, encode_ok=True):
    m = types.SimpleNamespace()
    m.CAP_V4L2 = 200
    m.CAP_PROP_FRAME_WIDTH = 3
    m.CAP_PROP_FRAME_HEIGHT = 4
    m.CAP_PROP_BUFFERSIZE = 38
    m.FONT_HERSHEY_SIMPLEX = 0
    m.LINE_AA = 16
    m.IMWRITE_JPEG_QUALITY = 1
    m.opened_with = []
    m.texts = []

    def VideoCapture(index, api):
        m.opened_with.append((index, api))
        return cap

    def putText(frame, text, *args, **kw):
        m.texts.append(text)

    def imencode(ext, frame, params=None):
        m.encoded = (ext, frame)
        return (encode_ok, FakeBuffer(b"\xff\xd8JPEGDATA\xff\xd9"))

    m.VideoCapture = VideoCapture
    m.putText = putText
    m.imencode = imencode
    return m


def install(monkeypatch, cv2):
    monkeypatch.setitem(sys.modules, "cv2", cv2)


def test_capture_writes_jpeg_and_releases(monkeypatch, tmp_path):
    cap = FakeCapture()
    cv2 = make_cv2(cap)
    install(monkeypatch, cv2)
    path = tmp_path / "anh" / "a.jpg"                   # thư mục cha chưa có
    assert camera.capture_jpeg(str(path), index=2, warmup_frames=5) is True
    assert path.read_bytes() == b"\xff\xd8JPEGDATA\xff\xd9"
    assert cv2.opened_with == [(2, cv2.CAP_V4L2)]
    assert cap.reads == 5                               # bỏ khung khởi động, dùng khung cuối
    assert cv2.encoded == (".jpg", ["frame", 5])
    assert cap.props[cv2.CAP_PROP_FRAME_WIDTH] == 640
    assert cap.props[cv2.CAP_PROP_FRAME_HEIGHT] == 480
    assert len(cv2.texts) == 1 and len(cv2.texts[0]) >= 16   # in giờ địa phương lên ảnh
    assert cap.released
    assert [p.name for p in path.parent.iterdir()] == ["a.jpg"]   # không còn file tạm


def test_capture_camera_not_opened(monkeypatch, tmp_path):
    cap = FakeCapture(opened=False)
    install(monkeypatch, make_cv2(cap))
    path = tmp_path / "a.jpg"
    assert camera.capture_jpeg(str(path)) is False
    assert not path.exists()
    assert cap.released


def test_capture_read_failure_releases(monkeypatch, tmp_path):
    for cap in (FakeCapture(frames_ok=False), FakeCapture(raise_on_read=True)):
        install(monkeypatch, make_cv2(cap))
        path = tmp_path / "a.jpg"
        assert camera.capture_jpeg(str(path)) is False
        assert not path.exists()
        assert cap.released


def test_capture_encode_failure(monkeypatch, tmp_path):
    cap = FakeCapture()
    install(monkeypatch, make_cv2(cap, encode_ok=False))
    path = tmp_path / "a.jpg"
    assert camera.capture_jpeg(str(path)) is False
    assert not path.exists() and cap.released


def test_capture_without_cv2(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "cv2", None)       # import cv2 -> ImportError
    assert camera.capture_jpeg(str(tmp_path / "a.jpg")) is False


def test_purge_photos_removes_only_old_jpegs(tmp_path):
    now = 1_800_000_000
    old = tmp_path / "old.jpg"
    new = tmp_path / "new.jpg"
    other = tmp_path / "ghi_chu.txt"
    for p in (old, new, other):
        p.write_bytes(b"x")
    os.utime(old, (now - 8 * 86400, now - 8 * 86400))
    os.utime(new, (now - 6 * 86400, now - 6 * 86400))
    os.utime(other, (now - 30 * 86400, now - 30 * 86400))
    assert camera.purge_photos(str(tmp_path), 7, now) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ghi_chu.txt", "new.jpg"]


def test_purge_photos_missing_dir(tmp_path):
    assert camera.purge_photos(str(tmp_path / "khong_co"), 7, 1_800_000_000) == 0


# ---------------- chụp trong tiến trình con (có timeout) ----------------
class FakeRunner:
    def __init__(self, result=0, write=True):
        self.result = result
        self.write = write
        self.calls = []

    def __call__(self, cmd, timeout):
        self.calls.append((cmd, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        if self.write and self.result == 0:
            path = cmd[cmd.index("aiot.camera") + 1]
            with open(path, "wb") as f:
                f.write(b"jpeg")
        return self.result


def test_capture_subprocess_success(tmp_path):
    path = str(tmp_path / "a.jpg")
    runner = FakeRunner()
    assert camera.capture_subprocess(path, index=2, runner=runner) is True
    cmd, timeout = runner.calls[0]
    assert cmd[0] == sys.executable and cmd[1:4] == ["-m", "aiot.camera", path]
    assert cmd[cmd.index("--index") + 1] == "2"
    assert timeout == camera.SUBPROCESS_TIMEOUT_S == 20


def test_capture_subprocess_timeout_and_failures(tmp_path):
    path = str(tmp_path / "a.jpg")
    for runner in (FakeRunner(subprocess.TimeoutExpired(["x"], 20)), FakeRunner(1),
                   FakeRunner(OSError("không chạy được python")), FakeRunner(0, write=False)):
        assert camera.capture_subprocess(path, runner=runner) is False


def test_default_runner_kills_hung_child():
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        camera._run_with_timeout([sys.executable, "-c", "import time; time.sleep(30)"], 1)
    assert time.monotonic() - start < 10


def test_default_runner_returns_exit_code():
    assert camera._run_with_timeout([sys.executable, "-c", "raise SystemExit(3)"], 20) == 3


def test_main_exit_codes(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(camera, "capture_jpeg",
                        lambda path, index=0: calls.append((path, index)) or True)
    assert camera.main([str(tmp_path / "a.jpg"), "--index", "1"]) == 0
    assert calls == [(str(tmp_path / "a.jpg"), 1)]
    monkeypatch.setattr(camera, "capture_jpeg", lambda path, index=0: False)
    assert camera.main([str(tmp_path / "a.jpg")]) == 1
