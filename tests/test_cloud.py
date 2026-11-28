"""Test web trên cloud (cloud/app.py) và vòng đầy đủ với aiot/uplink.py. Không dùng mạng."""
import dataclasses
import json
import os

import pytest

os.environ.setdefault("AIOT_CLOUD_NO_APP", "1")     # không tạo app mặc định khi import
fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from cloud.app import Settings, create_app, normalize_db_url  # noqa: E402
from cloud.security import (RateLimiter, check_session, hash_password, make_session,  # noqa: E402
                            verify_password)

TOKEN = "test-ingest-token"
PASSWORD = "matkhau-nhom"
HDR = {"Authorization": f"Bearer {TOKEN}"}
CMD_HDR = {"X-Requested-With": "aiot"}


class Clock:
    def __init__(self, t=1_800_000_000):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(tmp_path, clock):
    s = Settings(database_url=f"sqlite:///{tmp_path / 'cloud.db'}", ingest_token=TOKEN,
                 password_hash=hash_password(PASSWORD, iterations=1000),
                 session_secret="s" * 32, cookie_secure=False)
    return TestClient(create_app(s, clock=clock))


def login(client):
    r = client.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 200
    return r


# ---------------- security ----------------
def test_password_hash_roundtrip():
    h = hash_password("abc12345", iterations=1000)
    assert verify_password("abc12345", h)
    assert not verify_password("sai", h)
    assert not verify_password("abc12345", None)
    assert not verify_password("abc12345", "rác")


def test_session_sign_and_expiry():
    tok = make_session("k", 60, now=1000)
    assert check_session("k", tok, now=1059)
    assert not check_session("k", tok, now=1061)
    assert not check_session("khac", tok, now=1000)
    assert not check_session("k", tok[:-1] + ("0" if tok[-1] != "0" else "1"), now=1000)
    assert not check_session("k", "rác", now=1000)


def test_rate_limiter_window():
    t = [0.0]
    rl = RateLimiter(2, 60, clock=lambda: t[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")
    t[0] = 61
    assert rl.allow("a")


def test_normalize_db_url():
    assert normalize_db_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_db_url("postgresql://u@h/db?sslmode=require").startswith("postgresql+psycopg://")
    assert normalize_db_url("sqlite:///x.db") == "sqlite:///x.db"


# ---------------- API ----------------
def test_index_and_static_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Trạm quan trắc" in r.text
    assert client.get("/static/app.js").status_code == 200


def test_ingest_requires_token(client):
    assert client.post("/api/ingest", json={}).status_code == 401
    assert client.post("/api/ingest", json={}, headers={"Authorization": "Bearer sai"}).status_code == 401
    assert client.post("/api/ingest", json={}, headers=HDR).status_code == 200


def test_ingest_without_server_token_rejects_everything(tmp_path, clock):
    c = TestClient(create_app(Settings(database_url=f"sqlite:///{tmp_path / 'c.db'}"), clock=clock))
    assert c.post("/api/ingest", json={}, headers={"Authorization": "Bearer "}).status_code == 401


def test_status_before_and_after_ingest(client, clock):
    st = client.get("/api/status").json()
    assert st["snapshot"] is None and st["pi_online"] is False and st["logged_in"] is False
    client.post("/api/ingest", json={"snapshot": {"pm25": 12.5}}, headers=HDR)
    st = client.get("/api/status").json()
    assert st["snapshot"]["pm25"] == 12.5 and st["pi_online"] is True
    clock.t += 31
    assert client.get("/api/status").json()["pi_online"] is False


def test_history_forecast_events_stored(client, clock):
    t = clock.t
    body = {"history": [{"ts": t - 120, "pm25": 10, "label": "normal"},
                        {"ts": t - 60, "pm25": 20.5, "pm10": "x", "label": "cooking"},
                        {"pm25": 1}],
            "forecast": {"made_at": t - 100, "model": "seasonal_naive", "values": [30, 31, 32, 33, 34, 35]},
            "events": [{"id": 7, "ts": t - 50, "kind": "fan_on", "detail": "PM cao"}]}
    assert client.post("/api/ingest", json=body, headers=HDR).status_code == 200
    # gửi lại (Pi thử lại sau lỗi mạng) không nhân đôi
    assert client.post("/api/ingest", json=body, headers=HDR).status_code == 200
    rows = client.get("/api/history?hours=1").json()["rows"]
    assert [r["pm25"] for r in rows] == [10, 20.5]
    assert rows[1]["pm10"] is None and rows[1]["label"] == "cooking"
    fc = client.get("/api/forecast").json()
    assert fc["model"] == "seasonal_naive" and [v["pm25"] for v in fc["values"]][:2] == [30, 31]
    ev = client.get("/api/events").json()["rows"]
    assert len(ev) == 1 and ev[0]["kind"] == "fan_on"
    assert client.get("/api/history?hours=999").status_code == 422


def test_command_requires_login_and_header(client):
    assert client.post("/api/command", json={"kind": "fan", "value": "on"},
                       headers=CMD_HDR).status_code == 401
    login(client)
    assert client.get("/api/status").json()["logged_in"] is True
    assert client.post("/api/command", json={"kind": "fan", "value": "on"}).status_code == 403
    assert client.post("/api/command", json={"kind": "fan", "value": "auto"},
                       headers=CMD_HDR).status_code == 422
    assert client.post("/api/command", json={"kind": "door", "value": "on"},
                       headers=CMD_HDR).status_code == 422
    r = client.post("/api/command", json={"kind": "fan", "value": "on"}, headers=CMD_HDR)
    assert r.status_code == 200 and r.json()["pi_online"] is False and r.json()["note"]
    client.post("/api/logout")
    assert client.get("/api/status").json()["logged_in"] is False


def test_wrong_password_and_login_rate_limit(client):
    for _ in range(5):
        assert client.post("/api/login", json={"password": "sai"}).status_code == 401
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 429


def test_command_rate_limit(client):
    login(client)
    for _ in range(10):
        assert client.post("/api/command", json={"kind": "mode", "value": "auto"},
                           headers=CMD_HDR).status_code == 200
    assert client.post("/api/command", json={"kind": "mode", "value": "auto"},
                       headers=CMD_HDR).status_code == 429


def test_command_delivered_then_acked(client, clock):
    login(client)
    cid = client.post("/api/command", json={"kind": "buzzer", "value": "on"},
                      headers={**CMD_HDR, "X-Forwarded-For": "5.6.7.8, 10.0.0.1"}).json()["id"]
    clock.t += 3
    cmds = client.post("/api/ingest", json={}, headers=HDR).json()["commands"]
    assert cmds == [{"id": cid, "kind": "buzzer", "value": "on", "age_s": 3,
                     "source": "web ip=5.6.7.8"}]
    assert client.get("/api/commands").json()["rows"][0]["status"] == "sent"
    # chưa ack thì gửi lại (Pi tự bỏ trùng theo id)
    assert len(client.post("/api/ingest", json={}, headers=HDR).json()["commands"]) == 1
    client.post("/api/ingest", json={"acks": [{"id": cid, "status": "done", "detail": "ok"}]},
                headers=HDR)
    assert client.get("/api/commands").json()["rows"][0]["status"] == "done"
    assert client.post("/api/ingest", json={}, headers=HDR).json()["commands"] == []


def test_command_expires_when_pi_offline(client, clock):
    login(client)
    client.post("/api/command", json={"kind": "fan", "value": "off"}, headers=CMD_HDR)
    clock.t += 61
    assert client.get("/api/commands").json()["rows"][0]["status"] == "expired"
    assert client.post("/api/ingest", json={}, headers=HDR).json()["commands"] == []


# ---------------- vòng đầy đủ: web -> cloud -> uplink -> controller -> web ----------------
def test_end_to_end_with_uplink_and_controller(client, clock, tmp_path):
    from aiot import uplink
    from aiot.commands import BUZZER_OVERRIDE_KEY
    from aiot.config import CloudCfg, Config, Secrets
    from aiot.controller import step
    from aiot.db import RawSample, connect, get_state, init_schema, insert_raw

    t0 = clock.t
    cfg = dataclasses.replace(Config(), cloud=CloudCfg(enabled=True, url="http://testserver"),
                              secrets=Secrets(ingest_token=TOKEN))
    conn = connect(tmp_path / "aiot.db")
    init_schema(conn)
    for ts in range(t0 - 600, t0, 10):
        insert_raw(conn, RawSample(ts, 5.0, 20.0, 25.0, 27.0, 70.0, 100.0, True))

    class Relay:
        def set(self, on):
            self.on = on

    class Buzzer:
        bursts = []

        def burst(self, a, b, d):
            self.bursts.append(d)

        def silence(self):
            pass

    relay, buzzer = Relay(), Buzzer()
    up = uplink.Uplink(conn, cfg, session=client)
    assert up.once(t0) is True
    st = client.get("/api/status").json()
    assert st["snapshot"]["pm25"] == 20.0 and st["snapshot"]["control"]["mode"] == "auto"
    assert st["snapshot"]["aqi"]["label"] == "Trung bình"
    assert len(client.get("/api/history?hours=1").json()["rows"]) == 10

    login(client)
    client.post("/api/command", json={"kind": "mode", "value": "manual"}, headers=CMD_HDR)
    client.post("/api/command", json={"kind": "buzzer", "value": "on"}, headers=CMD_HDR)
    clock.t = t0 + 2
    assert up.once(t0 + 2) is True                 # nhận 2 lệnh, ghi khoá state
    assert len(up.acks) == 2
    step(conn, relay, cfg.control, t0 + 3, monotonic=lambda: 10_000, buzzer=buzzer,
         bcfg=cfg.buzzer)
    assert relay.on is False                       # thủ công giữ quạt đang tắt
    assert buzzer.bursts and buzzer.bursts[0] <= cfg.buzzer.manual_max_s
    assert json.loads(get_state(conn, BUZZER_OVERRIDE_KEY))["on"] is True
    clock.t = t0 + 4
    assert up.once(t0 + 4) is True                 # gửi ack + snapshot mới
    rows = client.get("/api/commands").json()["rows"]
    assert {r["status"] for r in rows} == {"done"}
    snap = client.get("/api/status").json()["snapshot"]
    assert snap["control"]["mode"] == "manual" and snap["buzzer"]["override"]["on"] is True
    # sự kiện lệnh lên cloud không kèm IP
    details = [e["detail"] for e in client.get("/api/events?limit=50").json()["rows"]
               if e["kind"] == "command"]
    assert details and all("ip=" not in d for d in details)
    conn.close()
