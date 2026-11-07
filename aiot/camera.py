"""
Chụp một ảnh JPEG từ camera USB (/dev/video0) bằng OpenCV, và xoá ảnh cũ.

- cv2 chỉ được import khi chụp: laptop chạy test không cần OpenCV, và bot/weather vẫn
  chạy (gửi chữ) khi venv trên Pi thiếu cv2.
- Mở camera mỗi lần chụp rồi luôn giải phóng, để không giữ /dev/video0. Nếu
  pi_camera_client.py đang chạy thì camera bận: capture_jpeg trả về False.
- Ghi file tạm rồi đổi tên, để bot không bao giờ gửi một ảnh đang ghi dở.
- Mọi lỗi (không có cv2, không mở được camera, đọc/nén lỗi, ghi file lỗi) chỉ ghi log và
  trả về False; không bao giờ raise.
- Dịch vụ (bot, weather) gọi capture_subprocess: chụp trong tiến trình con
  `python -m aiot.camera <path>` có timeout, vì ioctl UVC/USB bị treo có thể chặn mãi
  (bot sẽ không trả lời lệnh nào, kể cả /fan, mà systemd vẫn thấy tiến trình sống). Cách
  này cũng giữ OpenCV ngoài tiến trình bot chạy lâu dài.

Chạy tay: venv/bin/python -m aiot.camera /tmp/thu.jpg [--index 0]   (thoát 0 nếu chụp được)
"""
import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("aiot.camera")

JPEG_QUALITY = 80
DAY_S = 86400
SUBPROCESS_TIMEOUT_S = 20
KILL_WAIT_S = 3
REPO_ROOT = Path(__file__).resolve().parent.parent


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
    """Xoá file *.jpg và *.jpg.tmp (file tạm của tiến trình chụp bị kill) trong dir
    (không đệ quy) sửa đổi trước now - keep_days ngày.
    Trả về số file đã xoá; thư mục không có thì trả về 0."""
    folder = Path(dir)
    if not folder.is_dir():
        return 0
    cutoff = float(now) - float(keep_days) * DAY_S
    removed = 0
    for p in [*folder.glob("*.jpg"), *folder.glob("*.jpg.tmp")]:
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError as e:
            log.warning("Không xoá được ảnh cũ %s: %s", p.name, e)
    return removed


def _run_with_timeout(cmd, timeout):
    """Chạy cmd, trả về mã thoát; quá timeout thì kill và raise subprocess.TimeoutExpired.

    Không dùng subprocess.run: sau khi kill, run() chờ tiến trình con không giới hạn, mà
    tiến trình kẹt trong ioctl của driver (trạng thái D) có thể không chết ngay. Ở đây chỉ
    chờ thêm KILL_WAIT_S rồi bỏ mặc nó."""
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=None)
    # stderr kế thừa: cảnh báo của tiến trình con (log của capture_jpeg) vào journald
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            log.error("Tiến trình chụp ảnh (pid %s) không dừng sau khi kill", proc.pid)
        raise


def capture_subprocess(path, index=0, *, timeout=SUBPROCESS_TIMEOUT_S,
                       runner=_run_with_timeout) -> bool:
    """capture_jpeg trong tiến trình con có timeout. True khi mã thoát 0 và file đã có.
    runner(cmd, timeout) -> mã thoát (thay được trong test)."""
    cmd = [sys.executable, "-m", "aiot.camera", str(path), "--index", str(int(index))]
    try:
        code = runner(cmd, timeout)
    except subprocess.TimeoutExpired:
        log.warning("Camera không phản hồi sau %s giây, đã dừng tiến trình chụp", timeout)
        return False
    except OSError as e:
        log.warning("Không chạy được tiến trình chụp ảnh: %s", e)
        return False
    if code != 0:
        log.warning("Tiến trình chụp ảnh thoát mã %s", code)
        return False
    return os.path.isfile(path)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Chụp một ảnh JPEG từ camera USB")
    parser.add_argument("path")
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args(argv)
    return 0 if capture_jpeg(args.path, index=args.index) else 1


if __name__ == "__main__":
    raise SystemExit(main())
