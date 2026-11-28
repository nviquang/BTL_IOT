"""Test aiot/uplink.py với server giả: payload, lệnh trùng id, lệnh cũ, mất mạng."""
import dataclasses
import json

import pytest

from aiot import uplink
from aiot.config import BuzzerCfg, CloudCfg, Config, Secrets
from aiot.db import RawSample, connect, get_state, init_schema, insert_raw, log_event, set_state

NOW = 1_800_000_000
CFG = dataclasses.replace(Config(), cloud=CloudCfg(enabled=True, url="https://x.example"),
                          secrets=Secrets(ingest_token="tok"))


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    yield c
    c.close()


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {"commands": []}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        r = self.responses.pop(0) if self.responses else Resp()
        if isinstance(r, Exception):
            raise r
        return r


def add(conn, t_from, t_to, pm25=20.0):
    for ts in range(t_from, t_to, 10):
        insert_raw(conn, RawSample(ts, 5.0, pm25, 25.0, 27.0, 70.0, 100.0, True))


def cmd(cid, kind="fan", value="on", age=2):
    return {"id": cid, "kind": kind, "value": value, "age_s": age, "source": "web ip=1.2.3.4"}


def test_payload_and_cursor_advance(conn):
    add(conn, NOW - 300, NOW + 1)
    log_event(conn, NOW - 5, "command", "/fan on chat=111")
    s = Session()
    up = uplink.Uplink(conn, CFG, session=s)
    assert up.once(NOW) is True
    call = s.calls[0]
    assert call["url"] == "https://x.example/api/ingest"
    assert call["headers"]["Authorization"] == "Bearer tok"
    p = call["json"]
    assert p["snapshot"]["pm25"] == 20.0 and p["snapshot"]["ts"] == NOW
    assert len(p["history"]) == 5 and p["history"][0]["ts"] % 60 == 0
    assert p["events"][0]["detail"] == "/fan on"          # chat id bị bỏ
    # lần sau không gửi lại history, sự kiện cũ
    up.once(NOW + 5)
    p2 = s.calls[1]["json"]
    assert p2["history"] == [] and p2["events"] == []
    assert get_state(conn, uplink.LAST_OK_KEY) == str(NOW + 5)


def test_network_error_keeps_cursor_and_resends(conn):
    import requests
    add(conn, NOW - 120, NOW)
    s = Session(requests.ConnectionError("mất mạng"), requests.ConnectionError("mất mạng"), Resp(500))
    up = uplink.Uplink(conn, CFG, session=s)
    assert up.once(NOW) is False
    assert up.once(NOW) is False
    assert get_state(conn, uplink.CURSOR_KEY) is None
    assert up.once(NOW) is True
    assert len(s.calls[2]["json"]["history"]) == 2


def test_replaces_retired_forecast_on_cloud_in_same_hour(conn):
    set_state(conn, uplink.CURSOR_KEY, json.dumps({"forecast_at": NOW}))
    conn.executemany("INSERT INTO forecasts VALUES (?, ?, ?, ?)",
                     [(NOW, h, 20.0, "retired_method") for h in range(1, 7)])
    conn.commit()
    session = Session()
    up = uplink.Uplink(conn, CFG, session=session)
    assert up.once(NOW) is True
    assert session.calls[-1]["json"]["forecast"] is None

    conn.execute("UPDATE forecasts SET model = 'persistence' WHERE made_at = ?", (NOW,))
    conn.commit()
    assert up.once(NOW + 5) is True
    assert session.calls[-1]["json"]["forecast"] == {
        "made_at": NOW, "model": "persistence", "values": [20.0] * 6,
    }
    assert up.once(NOW + 10) is True
    assert session.calls[-1]["json"]["forecast"] is None


def test_bad_json_response_is_failure(conn):
    up = uplink.Uplink(conn, CFG, session=Session(Resp(200, ValueError("x"))))
    assert up.once(NOW) is False


def test_command_applied_once_and_acked(conn):
    s = Session(Resp(body={"commands": [cmd("a1")]}), Resp(body={"commands": [cmd("a1")]}), Resp())
    up = uplink.Uplink(conn, CFG, session=s)
    up.once(NOW)
    pending = json.loads(get_state(conn, "pending_override"))
    assert pending["fan"] is True and pending["id"] == "web:a1" and pending["at"] == NOW
    assert up.acks == [{"id": "a1", "status": "done", "detail": up.acks[0]["detail"]}]
    conn.execute("DELETE FROM state WHERE key='pending_override'")   # controller đã áp dụng
    conn.commit()
    up.once(NOW + 1)                                  # cloud gửi lại cùng id
    assert get_state(conn, "pending_override") is None
    assert s.calls[1]["json"]["acks"][0]["id"] == "a1"
    up.once(NOW + 2)
    assert s.calls[2]["json"]["acks"][0]["status"] == "done"
    events = [r[0] for r in conn.execute("SELECT detail FROM events WHERE kind='command'")]
    assert events == ["/fan on (web) web ip=1.2.3.4"]


@pytest.mark.parametrize("bad, reason", [
    (cmd("b1", age=61), "lệnh quá cũ"),
    (cmd("b2", age=None), "lệnh quá cũ"),
    (cmd("b3", kind="door"), "lệnh không hợp lệ"),
    (cmd("b4", kind="fan", value="auto"), "lệnh không hợp lệ"),
])
def test_bad_commands_rejected(conn, bad, reason):
    up = uplink.Uplink(conn, CFG, session=Session(Resp(body={"commands": [bad]})))
    up.once(NOW)
    assert up.acks == [{"id": bad["id"], "status": "rejected", "detail": reason}]
    assert get_state(conn, "pending_override") is None


def test_buzzer_rejected_when_disabled(conn):
    cfg = dataclasses.replace(CFG, buzzer=BuzzerCfg(enabled=False))
    up = uplink.Uplink(conn, cfg, session=Session(Resp(body={"commands": [cmd("c", "buzzer")]})))
    up.once(NOW)
    assert up.acks[0]["status"] == "rejected"


def test_command_without_id_ignored(conn):
    up = uplink.Uplink(conn, CFG, session=Session(Resp(body={"commands": [{"kind": "fan"}, "x"]})))
    up.once(NOW)
    assert up.acks == []


def test_snapshot_reports_manual_mode_and_source(conn):
    from aiot.commands import BUZZER_OVERRIDE_KEY
    from aiot.db import insert_prediction, set_state
    add(conn, NOW - 30, NOW + 1, pm25=60.0)
    set_state(conn, BUZZER_OVERRIDE_KEY, json.dumps({"on": True, "until": NOW + 30, "at": NOW}))
    set_state(conn, "source_state", json.dumps({"label": "cooking", "since": NOW - 100,
                                                "last_ts": NOW - 2, "history": []}))
    insert_prediction(conn, NOW - 2, "cooking", "cooking",
                      json.dumps({"normal": 0.1, "cooking": 0.8, "pollution": 0.05, "smoking": 0.05}),
                      "rf")
    snap = uplink.build_snapshot(conn, CFG, NOW)
    assert snap["control"]["mode"] == "manual"
    assert snap["buzzer"]["override"] == {"on": True, "until": NOW + 30}
    assert snap["source"]["label"] == "cooking" and snap["source"]["fresh"] is True
    assert snap["source"]["proba"]["cooking"] == 0.8
    assert snap["aqi"]["label"] == "Xấu"


def test_history_labels_and_incomplete_window(conn):
    from aiot.db import insert_prediction
    add(conn, NOW - 120, NOW + 30)
    insert_prediction(conn, NOW - 110, "smoking", "smoking", None, "rf")
    rows, nxt = uplink.build_history(conn, NOW - 120, NOW + 30, 60)
    assert [r["ts"] for r in rows] == [NOW - 120, NOW - 60]     # cửa sổ [NOW, NOW+60) chưa đủ
    assert rows[0]["label"] == "smoking" and rows[1]["label"] is None
    assert nxt == NOW


def test_main_refuses_without_config(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("cloud:\n  enabled: false\n", encoding="utf-8")
    assert uplink.main(["--config", str(p)]) == 2
    p.write_text("cloud:\n  enabled: true\n  url: http://example.com\n", encoding="utf-8")
    monkeypatch.setenv("INGEST_TOKEN", "x")
    assert uplink.main(["--config", str(p)]) == 2               # không phải https


def test_connection_error_retried_once(conn):
    import requests
    s = Session(requests.ConnectionError("keep-alive vừa đóng"), Resp())
    assert uplink.Uplink(conn, CFG, session=s).once(NOW) is True
    assert len(s.calls) == 2
