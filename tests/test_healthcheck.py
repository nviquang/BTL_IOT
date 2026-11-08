"""Test bảng kiểm tra sức khoẻ (aiot/healthcheck.py): mọi phụ thuộc ngoài đều giả."""
import dataclasses
import subprocess

import pytest

from aiot import healthcheck as hc
from aiot.config import Config, Secrets
from aiot.db import RawSample, connect, init_schema, insert_raw, set_state
from aiot.telegram_api import TelegramError

NOW = 1_800_000_000
TOKEN = "123456789:AAH-secret_TokenValue-xyz"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "aiot.db"
    conn = connect(path)
    init_schema(conn)
    conn.close()
    return path


def add_raw(db_path, ts, **values):
    fields = dict(pm1=10.0, pm25=20.0, pm10=30.0, temp=28.0, hum=70.0, lux=100.0, mq135_do=1)
    fields.update(values)
    conn = connect(db_path)
    insert_raw(conn, RawSample(ts=ts, clock_synced=True, **fields))
    conn.close()


# ---------------- .env ----------------
def test_env_all_keys_present(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_TOKEN=a\nGEMINI_API_KEY=b\nALLOWED_CHAT_IDS=1\nMQTT_PASSWORD=c\n",
                   encoding="utf-8")
    check = hc.check_env(env)
    assert check.ok is True
    assert not check.critical


def test_env_reports_missing_and_empty_keys_without_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_TOKEN=supersecretvalue\nGEMINI_API_KEY=\nALLOWED_CHAT_IDS=1\n",
                   encoding="utf-8")
    check = hc.check_env(env)
    assert check.ok is False
    assert "GEMINI_API_KEY" in check.detail
    assert "MQTT_PASSWORD" in check.detail
    assert "TELEGRAM_TOKEN" not in check.detail
    assert "supersecretvalue" not in check.detail


def test_env_file_missing(tmp_path):
    check = hc.check_env(tmp_path / ".env")
    assert check.ok is False
    assert ".env" in check.detail


# ---------------- systemd ----------------
def test_units_states_and_criticality():
    states = {"aiot-collector": "active", "aiot-controller": "failed", "aiot-bot": "active",
              "aiot-forecast.timer": "active", "aiot-morning.timer": "inactive",
              "aiot-weather.timer": "active"}
    checks = hc.check_units(hc.UNITS, runner=lambda unit: states[unit])
    by_name = {c.name: c for c in checks}
    assert by_name["aiot-collector"].ok is True
    assert by_name["aiot-collector"].critical
    assert by_name["aiot-controller"].ok is False
    assert not by_name["aiot-controller"].critical
    assert "failed" in by_name["aiot-controller"].detail
    assert by_name["aiot-morning.timer"].ok is False


def test_units_collector_inactive_is_critical_failure():
    checks = hc.check_units(["aiot-collector"], runner=lambda unit: "inactive")
    assert checks[0].ok is False and checks[0].critical
    assert hc.exit_code(checks) == 1


def test_units_skipped_are_not_queried():
    asked = []

    def runner(unit):
        asked.append(unit)
        return "active"

    checks = hc.check_units(["aiot-collector", "aiot-controller"], runner=runner,
                            skip={"aiot-controller"})
    assert asked == ["aiot-collector"]
    assert checks[1].ok is None


def test_systemctl_runner_handles_missing_binary(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("systemctl")
    monkeypatch.setattr(hc.subprocess, "run", boom)
    assert hc.systemctl_is_active("aiot-collector") == "không có systemctl"


# ---------------- dữ liệu mới nhất ----------------
def test_fresh_data_ok_and_all_sensors_present(db_path):
    add_raw(db_path, NOW - 15)
    fresh, sensors = hc.check_latest_data(db_path, NOW)
    assert fresh.ok is True and fresh.critical
    assert sensors.ok is True and not sensors.critical


def test_stale_data_is_critical_failure(db_path):
    add_raw(db_path, NOW - 300)
    fresh, _ = hc.check_latest_data(db_path, NOW)
    assert fresh.ok is False and fresh.critical
    assert hc.exit_code([fresh]) == 1


def test_data_reports_which_sensors_are_none(db_path):
    add_raw(db_path, NOW - 5, pm1=None, pm25=None, pm10=None, lux=None, mq135_do=None)
    fresh, sensors = hc.check_latest_data(db_path, NOW)
    assert fresh.ok is True
    assert sensors.ok is False
    assert "PMS7003" in sensors.detail
    assert "BH1750" in sensors.detail
    assert "MQ135" in sensors.detail
    assert "DHT11" not in sensors.detail


def test_data_missing_db_does_not_create_it(tmp_path):
    path = tmp_path / "none" / "aiot.db"
    fresh, sensors = hc.check_latest_data(path, NOW)
    assert fresh.ok is False and fresh.critical
    assert sensors.ok is None
    assert not path.exists()


def test_data_empty_table(db_path):
    fresh, sensors = hc.check_latest_data(db_path, NOW)
    assert fresh.ok is False
    assert sensors.ok is None


# ---------------- controller ----------------
def test_heartbeat_fresh(db_path):
    conn = connect(db_path)
    set_state(conn, hc.HEARTBEAT_KEY, NOW - 10)
    conn.close()
    assert hc.check_heartbeat(db_path, NOW, loop_s=20).ok is True


def test_heartbeat_stale_or_missing(db_path):
    assert hc.check_heartbeat(db_path, NOW, loop_s=20).ok is False
    conn = connect(db_path)
    set_state(conn, hc.HEARTBEAT_KEY, NOW - 600)
    conn.close()
    check = hc.check_heartbeat(db_path, NOW, loop_s=20)
    assert check.ok is False and not check.critical


def test_heartbeat_skipped():
    assert hc.check_heartbeat("/nonexistent.db", NOW, loop_s=20, skipped=True).ok is None


# ---------------- Telegram ----------------
class FakeClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.timeout = None

    def get_me(self, timeout):
        self.timeout = timeout
        if self.error:
            raise self.error
        return self.result


def test_telegram_ok():
    client = FakeClient(result={"username": "tram_bot"})
    check = hc.check_telegram(TOKEN, client_factory=lambda token: client)
    assert check.ok is True
    assert "@tram_bot" in check.detail
    assert client.timeout == 10


def test_telegram_skipped_without_token():
    check = hc.check_telegram(None, client_factory=lambda token: pytest.fail("không được gọi"))
    assert check.ok is None


def test_telegram_error_is_redacted():
    client = FakeClient(error=RuntimeError(f"boom https://api.telegram.org/bot{TOKEN}/getMe"))
    check = hc.check_telegram(TOKEN, client_factory=lambda token: client)
    assert check.ok is False
    assert TOKEN not in check.detail
    client = FakeClient(error=TelegramError("getMe: HTTP 401 Unauthorized", status=401))
    check = hc.check_telegram(TOKEN, client_factory=lambda token: client)
    assert "401" in check.detail


# ---------------- camera ----------------
def test_camera_ok_and_temp_file_removed(tmp_path):
    seen = {}

    def capture(path, index):
        seen["path"] = path
        open(path, "wb").write(b"jpeg")
        return True

    check = hc.check_camera(capture, legacy_running=lambda: False, index=0, tmp_dir=tmp_path)
    assert check.ok is True
    assert not (tmp_path / seen["path"]).exists()


def test_camera_failure():
    check = hc.check_camera(lambda path, index: False, legacy_running=lambda: False, index=0)
    assert check.ok is False and not check.critical


def test_camera_skipped_when_legacy_client_runs():
    check = hc.check_camera(lambda p, i: pytest.fail("không được chụp"),
                            legacy_running=lambda: True, index=0)
    assert check.ok is None
    assert "pi_camera_client" in check.detail


def test_legacy_camera_running_uses_pgrep(monkeypatch):
    monkeypatch.setattr(hc.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="123\n"))
    assert hc.legacy_camera_running() is True
    monkeypatch.setattr(hc.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=""))
    assert hc.legacy_camera_running() is False


# ---------------- đồng hồ ----------------
def test_clock():
    assert hc.check_clock(lambda: True).ok is True
    check = hc.check_clock(lambda: False)
    assert check.ok is False and not check.critical


# ---------------- tổng hợp ----------------
def test_exit_code_only_counts_critical():
    checks = [hc.Check("a", True, "", critical=True), hc.Check("b", False, "", critical=False),
              hc.Check("c", None, "", critical=True)]
    assert hc.exit_code(checks) == 0
    checks.append(hc.Check("d", False, "", critical=True))
    assert hc.exit_code(checks) == 1


def test_format_table_symbols():
    text = hc.format_table([hc.Check("thu thập", True, "ok", critical=True),
                            hc.Check("collector", False, "inactive", critical=True),
                            hc.Check("camera", False, "lỗi"),
                            hc.Check("bot", None, "bỏ qua")])
    lines = text.splitlines()
    assert lines[0].startswith("✅") and "thu thập" in lines[0]
    assert lines[1].startswith("❌")
    assert lines[2].startswith("⚠️")
    assert lines[3].startswith("➖")


def test_run_checks_skips_bot_without_chat_ids(db_path, tmp_path):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    add_raw(db_path, NOW - 5)
    cfg = dataclasses.replace(Config(db_path=str(db_path)),
                              secrets=Secrets(telegram_token=TOKEN))
    checks = hc.run_checks(cfg, env_path=env, now=NOW, runner=lambda unit: "active",
                           client_factory=lambda token: FakeClient(result={"username": "x"}),
                           capture=lambda p, i: False, legacy_running=lambda: False,
                           clock_trusted=lambda: True)
    by_name = {c.name: c for c in checks}
    assert by_name["aiot-bot"].ok is None
    assert hc.exit_code(checks) == 0


def test_run_checks_skip_controller(db_path, tmp_path):
    cfg = Config(db_path=str(db_path))
    asked = []

    def runner(unit):
        asked.append(unit)
        return "active"

    checks = hc.run_checks(cfg, env_path=tmp_path / ".env", now=NOW, runner=runner,
                           client_factory=lambda token: pytest.fail("không có token"),
                           capture=lambda p, i: True, legacy_running=lambda: False,
                           clock_trusted=lambda: True, skip_units={"aiot-controller"})
    assert "aiot-controller" not in asked
    heartbeat = [c for c in checks if c.name == "controller heartbeat"][0]
    assert heartbeat.ok is None
    assert hc.exit_code(checks) == 1   # chưa có dữ liệu


# ---------------- gợi ý nhóm video ----------------
class FakeGroup:
    def __init__(self, gid, members):
        self.gr_gid = gid
        self.gr_mem = members


def test_video_hint_none_when_session_has_group():
    hint = hc.video_group_hint(user="admin", getgroups=lambda: [44, 1000],
                               getgrnam=lambda name: FakeGroup(44, ["admin"]))
    assert hint is None


def test_video_hint_relogin_when_added_but_session_old():
    hint = hc.video_group_hint(user="admin", getgroups=lambda: [1000],
                               getgrnam=lambda name: FakeGroup(44, ["admin"]))
    assert "đăng nhập lại SSH" in hint


def test_video_hint_not_member():
    hint = hc.video_group_hint(user="admin", getgroups=lambda: [1000],
                               getgrnam=lambda name: FakeGroup(44, []))
    assert "video" in hint and "đăng nhập lại" not in hint


def test_video_hint_no_group_module():
    def missing(name):
        raise KeyError(name)
    assert hc.video_group_hint(user="admin", getgroups=lambda: [], getgrnam=missing) is None


def test_camera_failure_appends_hint():
    check = hc.check_camera(lambda p, i: False, legacy_running=lambda: False, index=0,
                            hint=lambda: "đăng nhập lại SSH")
    assert check.ok is False
    assert "đăng nhập lại SSH" in check.detail
    ok = hc.check_camera(lambda p, i: open(p, "wb").write(b"x") > 0, legacy_running=lambda: False,
                         index=0, hint=lambda: pytest.fail("không cần gợi ý khi chụp được"))
    assert ok.ok is True
