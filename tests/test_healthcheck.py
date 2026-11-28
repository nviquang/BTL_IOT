"""Test bảng kiểm tra sức khoẻ (aiot/healthcheck.py): mọi phụ thuộc ngoài đều giả."""
import dataclasses
import subprocess

import pytest

from aiot import healthcheck as hc
from aiot.config import Config, Secrets
from aiot.db import RawSample, connect, init_schema, insert_raw, set_state

NOW = 1_800_000_000


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "aiot.db"
    conn = connect(path)
    init_schema(conn)
    conn.close()
    return path


def add_raw(db_path, ts, **values):
    fields = dict(pm1=10.0, pm25=20.0, pm10=30.0, temp=28.0, hum=70.0, lux=100.0)
    fields.update(values)
    conn = connect(db_path)
    insert_raw(conn, RawSample(ts=ts, clock_synced=True, **fields))
    conn.close()


# ---------------- .env ----------------
def test_env_all_keys_present(tmp_path):
    env = tmp_path / ".env"
    env.write_text("INGEST_TOKEN=c\n", encoding="utf-8")
    check = hc.check_env(env)
    assert check.ok is True
    assert not check.critical


def test_env_reports_missing_and_empty_keys_without_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("INGEST_TOKEN=\nOTHER=supersecretvalue\n", encoding="utf-8")
    check = hc.check_env(env)
    assert check.ok is False
    assert "INGEST_TOKEN" in check.detail
    assert "supersecretvalue" not in check.detail


def test_env_file_missing(tmp_path):
    check = hc.check_env(tmp_path / ".env")
    assert check.ok is False
    assert ".env" in check.detail


# ---------------- systemd ----------------
def test_units_states_and_criticality():
    states = {"aiot-collector": "active", "aiot-controller": "failed", "aiot-uplink": "active",
              "aiot-forecast.timer": "inactive"}
    checks = hc.check_units(hc.UNITS, runner=lambda unit: states[unit])
    by_name = {c.name: c for c in checks}
    assert by_name["aiot-collector"].ok is True
    assert by_name["aiot-collector"].critical
    assert by_name["aiot-controller"].ok is False
    assert not by_name["aiot-controller"].critical
    assert "failed" in by_name["aiot-controller"].detail
    assert by_name["aiot-forecast.timer"].ok is False
    assert set(by_name) == {"aiot-collector", "aiot-controller", "aiot-uplink", "aiot-forecast.timer"}


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
    add_raw(db_path, NOW - 5, pm1=None, pm25=None, pm10=None, lux=None)
    fresh, sensors = hc.check_latest_data(db_path, NOW)
    assert fresh.ok is True
    assert sensors.ok is False
    assert "PMS7003" in sensors.detail
    assert "BH1750" in sensors.detail
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
                            hc.Check("đồng hồ", False, "lỗi"),
                            hc.Check("uplink", None, "bỏ qua")])
    lines = text.splitlines()
    assert lines[0].startswith("✅") and "thu thập" in lines[0]
    assert lines[1].startswith("❌")
    assert lines[2].startswith("⚠️")
    assert lines[3].startswith("➖")


def test_run_checks_has_no_telegram_or_camera(db_path, tmp_path):
    add_raw(db_path, NOW - 5)
    checks = hc.run_checks(Config(db_path=str(db_path)), env_path=tmp_path / ".env", now=NOW,
                           runner=lambda unit: "active", clock_trusted=lambda: True)
    names = {c.name for c in checks}
    assert not any("Telegram" in n or "camera" in n or "bot" in n for n in names)
    assert hc.exit_code(checks) == 0


def test_run_checks_skip_controller(db_path, tmp_path):
    cfg = Config(db_path=str(db_path))
    asked = []

    def runner(unit):
        asked.append(unit)
        return "active"

    checks = hc.run_checks(cfg, env_path=tmp_path / ".env", now=NOW, runner=runner,
                           clock_trusted=lambda: True, skip_units={"aiot-controller"})
    assert "aiot-controller" not in asked
    heartbeat = [c for c in checks if c.name == "controller heartbeat"][0]
    assert heartbeat.ok is None
    assert hc.exit_code(checks) == 1   # chưa có dữ liệu


def test_uplink_check(tmp_path):
    import dataclasses
    import sqlite3
    from aiot.config import CloudCfg, Config, Secrets
    db = tmp_path / "a.db"
    off = Config()
    assert hc.check_uplink(str(db), NOW, off).ok is None
    cfg = dataclasses.replace(Config(), cloud=CloudCfg(enabled=True, url="https://x"),
                              secrets=Secrets(ingest_token="t"))
    assert hc.check_uplink(str(db), NOW, cfg).ok is False
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO state VALUES ('uplink_last_ok', ?)", (str(NOW - 10),))
    c.commit()
    c.close()
    assert hc.check_uplink(str(db), NOW, cfg).ok is True
    assert hc.check_uplink(str(db), NOW + 100, cfg).ok is False
