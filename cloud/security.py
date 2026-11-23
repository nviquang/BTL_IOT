"""
Mật khẩu web, cookie phiên đã ký và giới hạn tần suất. Chỉ dùng thư viện chuẩn.

- Mật khẩu lưu dạng hash PBKDF2-SHA256 trong biến môi trường WEB_PASSWORD_HASH
  ("pbkdf2_sha256$<số vòng>$<salt hex>$<hash hex>"). Tạo hash:
      python -m cloud.security
  (nhập mật khẩu, không hiện trên màn hình; in ra chuỗi hash để dán vào Render).
- Cookie phiên: "<hết hạn>.<nonce>.<chữ ký HMAC-SHA256>" ký bằng SESSION_SECRET. Đổi
  SESSION_SECRET là đăng xuất mọi người; đổi WEB_PASSWORD_HASH là thu hồi mật khẩu cũ.
"""
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

ITERATIONS = 200_000


def hash_password(password: str, *, iterations: int = ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not isinstance(password, str):
        return False
    try:
        algo, iterations, salt_hex, digest_hex = stored.strip().split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session(secret: str, ttl_s: int, now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + ttl_s)
    payload = f"{exp}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(secret, payload)}"


def check_session(secret: str, token: str | None, now: float | None = None) -> bool:
    if not secret or not token:
        return False
    try:
        exp, nonce, sig = token.split(".")
        payload = f"{exp}.{nonce}"
        if not hmac.compare_digest(_sign(secret, payload), sig):
            return False
        return int(exp) > (now if now is not None else time.time())
    except ValueError:
        return False


class RateLimiter:
    """Tối đa `limit` lần mỗi `window_s` giây cho mỗi khoá (IP). Một tiến trình là đủ:
    Render free chạy một instance."""

    def __init__(self, limit: int, window_s: float = 60.0, clock=time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:        # không để bảng IP phình mãi
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True


if __name__ == "__main__":
    import getpass
    pw = getpass.getpass("Mật khẩu web: ")
    if len(pw) < 8:
        raise SystemExit("Mật khẩu cần ít nhất 8 ký tự.")
    if pw != getpass.getpass("Nhập lại: "):
        raise SystemExit("Hai lần nhập không khớp.")
    print(hash_password(pw))
