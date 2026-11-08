"""
Bảng kiểm tra sức khoẻ của trạm (scripts/pi_up.sh gọi sau khi bật dịch vụ).

- Không cần sudo, không in giá trị bí mật (chỉ tên khoá thiếu; lỗi Telegram đã che token).
- Quan trọng (quyết định mã thoát): aiot-collector đang chạy và có mẫu thô mới (< 60 s).
  Mọi mục khác chỉ là cảnh báo.
- Mở DB chỉ đọc: chưa có DB thì báo lỗi, không tạo file mới.
- Camera: chụp thử bằng tiến trình con có timeout (aiot.camera.capture_subprocess); bỏ qua
  khi client camera cũ (pi_camera_client.py / pi_detection_client.py) đang giữ camera.

Chạy: cd ~/BTL_IOT && venv/bin/python -m aiot.healthcheck [--skip-unit aiot-controller]
Mã thoát 0 khi mọi mục quan trọng đạt, 1 khi có mục quan trọng lỗi.
"""
import argparse
import os
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from aiot.config import load_config, read_env_file

ENV_KEYS = ("TELEGRAM_TOKEN", "GEMINI_API_KEY", "ALLOWED_CHAT_IDS", "MQTT_PASSWORD")
UNITS = ("aiot-collector", "aiot-controller", "aiot-bot",
         "aiot-forecast.timer", "aiot-morning.timer", "aiot-weather.timer")
CRITICAL_UNITS = frozenset({"aiot-collector"})
FRESH_S = 60                         # mẫu thô mới hơn mức này mới coi là đang thu thập
HEARTBEAT_KEY = "controller_heartbeat"
HEARTBEAT_STALE_LOOPS = 3            # như bot: cũ hơn 3 vòng loop_s là controller không chạy
TELEGRAM_TIMEOUT_S = 10
LEGACY_CAMERA_PATTERN = r"pi_(camera|detection)_client\.py"

# Cột của raw_readings -> cảm biến tương ứng (theo thứ tự in)
SENSOR_COLUMNS = (
    ("PMS7003", ("pm1", "pm25", "pm10")),
    ("DHT11", ("temp", "hum")),
    ("BH1750", ("lux",)),
    ("MQ135", ("mq135_do",)),
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None                  # None = bỏ qua
    detail: str
    critical: bool = False


# ---------------- từng mục ----------------
def check_env(env_path) -> Check:
    """Khoá nào trong .env còn thiếu hoặc rỗng. Không bao giờ đưa giá trị vào kết quả."""
    if not os.path.isfile(env_path):
        return Check(".env", False, f"không có file {env_path}")
    values = read_env_file(env_path)
    missing = [key for key in ENV_KEYS if not values.get(key)]
    if missing:
        return Check(".env", False, "thiếu hoặc rỗng: " + ", ".join(missing))
    return Check(".env", True, "đủ 4 khoá")


def systemctl_is_active(unit) -> str:
    """Trạng thái của unit theo `systemctl is-active` (active, inactive, failed, ...)."""
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return "không có systemctl"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"lỗi {type(e).__name__}"
    return r.stdout.strip() or "unknown"


def check_units(units, runner=systemctl_is_active, skip=frozenset()) -> list[Check]:
    checks = []
    for unit in units:
        critical = unit in CRITICAL_UNITS
        if unit in skip:
            checks.append(Check(unit, None, "bỏ qua", critical))
            continue
        state = runner(unit)
        checks.append(Check(unit, state == "active", state, critical))
    return checks


def _connect_ro(db_path):
    """Mở DB chỉ đọc; None khi chưa có file."""
    path = Path(db_path)
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _fmt_age(seconds) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} giây"
    if seconds < 7200:
        return f"{seconds // 60} phút"
    return f"{seconds // 3600} giờ"


def check_latest_data(db_path, now) -> tuple[Check, Check]:
    """(dữ liệu mới, cảm biến thiếu) từ mẫu thô mới nhất trong raw_readings."""
    skipped = Check("cảm biến", None, "chưa có mẫu")
    try:
        conn = _connect_ro(db_path)
        if conn is None:
            return Check("dữ liệu mới", False, f"chưa có DB {db_path}", True), skipped
        try:
            columns = [c for _, cols in SENSOR_COLUMNS for c in cols]
            row = conn.execute(f"SELECT ts, {', '.join(columns)} FROM raw_readings "
                               "ORDER BY ts DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return Check("dữ liệu mới", False, f"lỗi đọc DB: {e}", True), skipped
    if row is None:
        return Check("dữ liệu mới", False, "raw_readings chưa có mẫu nào", True), skipped

    age = now - row[0]
    fresh = Check("dữ liệu mới", 0 <= age < FRESH_S, f"mẫu mới nhất cách đây {_fmt_age(age)}"
                  if age >= 0 else f"mẫu mới nhất ở tương lai ({_fmt_age(-age)})", True)
    values = dict(zip(columns, row[1:]))
    missing = [sensor for sensor, cols in SENSOR_COLUMNS
               if any(values[c] is None for c in cols)]
    if not missing:
        return fresh, Check("cảm biến", True, "PMS7003, DHT11, BH1750, MQ135 đều có số")
    note = " (MQ135 cần sấy 3 phút sau khi collector khởi động)" if "MQ135" in missing else ""
    return fresh, Check("cảm biến", False, "không có số: " + ", ".join(missing) + note)


def check_heartbeat(db_path, now, loop_s, skipped=False) -> Check:
    name = "controller heartbeat"
    if skipped:
        return Check(name, None, "bỏ qua (không chạy controller)")
    try:
        conn = _connect_ro(db_path)
        if conn is None:
            return Check(name, False, "chưa có DB")
        try:
            row = conn.execute("SELECT value FROM state WHERE key = ?",
                               (HEARTBEAT_KEY,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return Check(name, False, f"lỗi đọc DB: {e}")
    try:
        beat = int(row[0]) if row else None
    except ValueError:
        beat = None
    if beat is None:
        return Check(name, False, "chưa thấy lần chạy nào")
    age = now - beat
    return Check(name, age <= HEARTBEAT_STALE_LOOPS * loop_s, f"cách đây {_fmt_age(age)}")


def _telegram_client(token):
    from aiot.telegram_api import TelegramClient
    return TelegramClient(token)


def check_telegram(token, client_factory=_telegram_client) -> Check:
    name = "Telegram getMe"
    if not token:
        return Check(name, None, "bỏ qua (thiếu TELEGRAM_TOKEN)")
    from aiot.telegram_api import redact
    try:
        me = client_factory(token).get_me(timeout=TELEGRAM_TIMEOUT_S) or {}
    except Exception as e:  # noqa: BLE001 - mọi lỗi chỉ là một dòng cảnh báo
        return Check(name, False, redact(f"{type(e).__name__}: {e}", token))
    username = me.get("username") if isinstance(me, dict) else None
    return Check(name, True, f"bot @{username}" if username else "token hợp lệ")


def legacy_camera_running() -> bool:
    """pi_camera_client.py hoặc pi_detection_client.py đang chạy (giữ /dev/video0)."""
    try:
        r = subprocess.run(["pgrep", "-f", LEGACY_CAMERA_PATTERN],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _capture(path, index):
    from aiot.camera import capture_subprocess
    return capture_subprocess(path, index=index)


def check_camera(capture=_capture, legacy_running=legacy_camera_running, index=0,
                 tmp_dir=None) -> Check:
    name = "camera"
    if legacy_running():
        return Check(name, None, "bỏ qua: pi_camera_client/pi_detection_client đang giữ camera")
    fd, path = tempfile.mkstemp(prefix="healthcheck_", suffix=".jpg", dir=tmp_dir)
    os.close(fd)
    os.remove(path)            # capture_jpeg tự tạo file; còn file nghĩa là đã chụp được
    try:
        ok = bool(capture(path, index))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return Check(name, ok, f"chụp thử /dev/video{index} được" if ok
                 else f"không chụp được từ /dev/video{index}")


def _clock_trusted():
    from aiot.collector import is_clock_trusted
    return is_clock_trusted()


def check_clock(clock_trusted=_clock_trusted) -> Check:
    if clock_trusted():
        return Check("đồng hồ", True, "đã đồng bộ NTP")
    return Check("đồng hồ", False, "chưa đồng bộ NTP: chưa gộp 5 phút, lệnh /fan bị từ chối")


# ---------------- tổng hợp ----------------
def run_checks(cfg, env_path=".env", now=None, runner=systemctl_is_active,
               client_factory=_telegram_client, capture=_capture,
               legacy_running=legacy_camera_running, clock_trusted=_clock_trusted,
               skip_units=frozenset()) -> list[Check]:
    now = int(time.time()) if now is None else now
    skip = set(skip_units)
    secrets = cfg.secrets
    if not secrets.telegram_token or not secrets.allowed_chat_ids:
        skip.add("aiot-bot")       # bot tự thoát mã 2 khi thiếu token/chat id
    checks = [check_env(env_path)]
    checks += check_units(UNITS, runner=runner, skip=skip)
    checks += list(check_latest_data(cfg.db_path, now))
    checks.append(check_heartbeat(cfg.db_path, now, cfg.control.loop_s,
                                  skipped="aiot-controller" in skip))
    checks.append(check_telegram(secrets.telegram_token, client_factory=client_factory))
    checks.append(check_camera(capture, legacy_running, index=cfg.weather.camera_index))
    checks.append(check_clock(clock_trusted))
    return checks


def exit_code(checks) -> int:
    return 1 if any(c.critical and c.ok is False for c in checks) else 0


def symbol(check) -> str:
    if check.ok is None:
        return "➖"
    if check.ok:
        return "✅"
    return "❌" if check.critical else "⚠️"


def format_table(checks) -> str:
    width = max((len(c.name) for c in checks), default=0)
    return "\n".join(f"{symbol(c)} {c.name.ljust(width)}  {c.detail}" for c in checks)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Bảng kiểm tra sức khoẻ trạm AIoT")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--skip-unit", action="append", default=[],
                        help="unit cố ý không chạy (lặp lại được), ví dụ aiot-controller")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config, args.env)
    except (OSError, ValueError) as e:
        print(f"❌ Không đọc được cấu hình: {e}")
        return 1
    checks = run_checks(cfg, env_path=args.env, skip_units=set(args.skip_unit))
    print(format_table(checks))
    code = exit_code(checks)
    print()
    print("Kết quả: các mục quan trọng đều đạt." if code == 0 else
          "Kết quả: có mục quan trọng (❌) chưa đạt — xem journalctl -u aiot-collector -n 30.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
