"""Test aiot/commands.py: lệnh dùng chung cho bot và web."""
import dataclasses
import json

import pytest

from aiot import commands
from aiot.config import BuzzerCfg, Config
from aiot.control import ControlState
from aiot.db import connect, get_state, init_schema, set_state

NOW = 1_800_000_000
CFG = dataclasses.replace(Config(), buzzer=BuzzerCfg(manual_max_s=60))


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    yield c
    c.close()


def events(conn):
    return [r[0] for r in conn.execute("SELECT detail FROM events WHERE kind='command'")]


def test_set_fan_writes_pending_and_event(conn):
    ttl = commands.set_fan(conn, CFG, NOW, True, cmd_id="w1", detail="fan on web user=a ip=1.2.3.4")
    data = json.loads(get_state(conn, commands.PENDING_KEY))
    assert data == {"fan": True, "ttl_s": ttl, "id": "w1", "at": NOW}
    assert events(conn) == ["fan on web user=a ip=1.2.3.4"]


def test_set_buzzer_on_capped_by_manual_max(conn):
    ttl = commands.set_buzzer(conn, CFG, NOW, True, cmd_id=3, detail="x")
    assert ttl == 60
    assert json.loads(get_state(conn, commands.BUZZER_PENDING_KEY))["on"] is True


def test_set_buzzer_off_uses_override_ttl(conn):
    assert commands.set_buzzer(conn, CFG, NOW, False, cmd_id=3, detail="x") == CFG.control.override_ttl_s


def test_mode_auto_clears_fan_and_buzzer(conn):
    assert commands.set_mode(conn, CFG, NOW, "auto", cmd_id=1, detail="x") is None
    assert json.loads(get_state(conn, commands.PENDING_KEY))["fan"] is None
    assert json.loads(get_state(conn, commands.BUZZER_PENDING_KEY))["on"] is None


def test_mode_manual_keeps_current_fan(conn):
    set_state(conn, commands.STATE_KEY, ControlState(mode="PROTECT").to_json())
    assert commands.set_mode(conn, CFG, NOW, "manual", cmd_id=1, detail="x") is True
    assert json.loads(get_state(conn, commands.PENDING_KEY))["fan"] is True


def test_mode_invalid_raises(conn):
    with pytest.raises(ValueError):
        commands.set_mode(conn, CFG, NOW, "turbo", cmd_id=1, detail="x")


def test_buzzer_override_and_is_manual(conn):
    assert commands.buzzer_override(conn, NOW) is None
    assert commands.is_manual(conn, NOW) is False
    set_state(conn, commands.BUZZER_OVERRIDE_KEY,
              json.dumps({"on": False, "until": NOW + 10, "at": NOW}))
    assert commands.buzzer_override(conn, NOW) == (False, NOW + 10, NOW)
    assert commands.is_manual(conn, NOW) is True
    assert commands.buzzer_override(conn, NOW + 10) is None
    set_state(conn, commands.BUZZER_OVERRIDE_KEY, "hỏng")
    assert commands.buzzer_override(conn, NOW) is None
