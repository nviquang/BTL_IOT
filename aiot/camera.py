"""
Chụp một ảnh JPEG từ camera USB (/dev/video0) bằng OpenCV, và xoá ảnh cũ.

- cv2 chỉ được import khi chụp: laptop chạy test không cần OpenCV, và bot/weather vẫn
  chạy (gửi chữ) khi venv trên Pi thiếu cv2.
- Mở camera mỗi lần chụp rồi luôn giải phóng, để không giữ /dev/video0. Nếu
  pi_camera_client.py đang chạy thì camera bận: capture_jpeg trả về False.
- Ghi file tạm rồi đổi tên, để bot không bao giờ gửi một ảnh đang ghi dở.
- Mọi lỗi (không có cv2, không mở được camera, đọc/nén lỗi, ghi file lỗi) chỉ ghi log và
  trả về False; không bao giờ raise.
"""
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("aiot.camera")

JPEG_QUALITY = 80
DAY_S = 86400


def capture_jpeg(path, index=0, width=640, height=480, warmup_frames=5) -> bool:
    """Chụp một ảnh, in giờ địa phương lên góc ảnh, ghi JPEG vào path. True nếu ghi được."""
    cap = None
    tmp = None
    try:
        import cv2

        cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        if not cap.isOpened():
            log.warning("Không mở được camera %s (không có hoặc đang bị tiến trình khác dùng)",
                        index)
            return False
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        frame = None
        # Bỏ vài khung đầu để camera tự chỉnh sáng và xoá khung cũ trong bộ đệm
        for _ in range(max(1, int(warmup_frames))):
            ok, frame = cap.read()
            if not ok or frame is None:
                log.warning("Đọc khung hình từ camera %s lỗi", index)
                return False
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(frame, stamp, (10, 25), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (0, 255, 0), 2, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            log.warning("Nén JPEG lỗi")
            return False
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(buf.tobytes())
        os.replace(tmp, target)
        tmp = None
        return True
    except Exception as e:
        log.warning("Không chụp được ảnh: %s: %s", type(e).__name__, e)
        return False
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception as e:
                log.warning("Giải phóng camera lỗi: %s", e)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def purge_photos(dir, keep_days, now) -> int:
    """Xoá file *.jpg trong dir (không đệ quy) sửa đổi trước now - keep_days ngày.
    Trả về số file đã xoá; thư mục không có thì trả về 0."""
    folder = Path(dir)
    if not folder.is_dir():
        return 0
    cutoff = float(now) - float(keep_days) * DAY_S
    removed = 0
    for p in folder.glob("*.jpg"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError as e:
            log.warning("Không xoá được ảnh cũ %s: %s", p.name, e)
    return removed
