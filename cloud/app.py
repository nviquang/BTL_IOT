"""
Web trên cloud của trạm quan trắc không khí (deploy lên Render, dữ liệu ở Postgres).

- Pi (aiot/uplink.py) gọi POST /api/ingest mỗi vài giây với header
  "Authorization: Bearer <INGEST_TOKEN>": gửi snapshot hiện tại, history (TB 1 phút),
  dự báo mới, sự kiện mới và kết quả các lệnh đã nhận. Trả về các lệnh đang chờ.
- Ai cũng xem được: GET /api/status, /api/history, /api/forecast, /api/events, /api/commands.
- Điều khiển cần đăng nhập bằng mật khẩu chung (POST /api/login): POST /api/command
  {kind: mode|fan|buzzer, value}. Lệnh chờ Pi nhận; quá COMMAND_TTL_S giây chưa được nhận
  thì 'expired' và không bao giờ được áp dụng muộn.
- Web không chạm thiết bị: Pi tự áp dụng lệnh qua đúng đường lệnh Telegram (controller).

Biến môi trường: DATABASE_URL (mặc định SQLite cục bộ để thử), INGEST_TOKEN,
WEB_PASSWORD_HASH (python -m cloud.security), SESSION_SECRET, COMMAND_TTL_S (60).

Chạy thử: uvicorn cloud.app:app --reload
"""
import hmac
import json
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import (Column, Float, Integer, MetaData, String, Table, Text, create_engine,
                        delete, func, select, update)

from cloud.security import RateLimiter, check_session, make_session, verify_password

log = logging.getLogger("cloud")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE = "aiot_session"
SESSION_TTL_S = 12 * 3600
PI_ONLINE_S = 30
HISTORY_KEEP_S = 7 * 86400
EVENTS_KEEP = 500
COMMANDS_KEEP_S = 30 * 86400
PURGE_EVERY_S = 3600
VALID_VALUES = {"mode": ("auto", "manual"), "fan": ("on", "off"), "buzzer": ("on", "off")}

metadata = MetaData()
snapshot_t = Table("snapshot", metadata,
                   Column("id", Integer, primary_key=True),
                   Column("data", Text, nullable=False),
                   Column("last_seen", Integer, nullable=False))
history_t = Table("history", metadata,
                  Column("ts", Integer, primary_key=True),
                  Column("pm1", Float), Column("pm25", Float), Column("pm10", Float),
                  Column("temp", Float), Column("hum", Float), Column("lux", Float),
                  Column("label", String(20)))
forecasts_t = Table("forecasts", metadata,
                    Column("made_at", Integer, primary_key=True),
                    Column("horizon_h", Integer, primary_key=True),
                    Column("pm25_pred", Float),
                    Column("model", String(40)))
events_t = Table("events", metadata,
                 Column("id", Integer, primary_key=True, autoincrement=True),
                 Column("pi_id", Integer, unique=True),
                 Column("ts", Integer, index=True),
                 Column("kind", String(40)),
                 Column("detail", Text))
commands_t = Table("commands", metadata,
                   Column("id", String(40), primary_key=True),
                   Column("kind", String(10), nullable=False),
                   Column("value", String(10), nullable=False),
                   Column("ip", String(64)),
                   Column("created_at", Integer, nullable=False, index=True),
                   Column("status", String(10), nullable=False),   # pending|sent|done|rejected|expired
                   Column("done_at", Integer),
                   Column("detail", Text))


@dataclass
class Settings:
    database_url: str = "sqlite:///./cloud_dev.db"
    ingest_token: str | None = None
    password_hash: str | None = None
    session_secret: str = field(default_factory=lambda: secrets.token_hex(32))
    command_ttl_s: int = 60
    cookie_secure: bool = True

    @classmethod
    def from_env(cls):
        url = os.environ.get("DATABASE_URL") or cls.database_url
        secret = os.environ.get("SESSION_SECRET")
        if not secret:
            log.warning("Chưa đặt SESSION_SECRET: dùng khoá ngẫu nhiên, khởi động lại là đăng xuất hết")
        return cls(database_url=url,
                   ingest_token=os.environ.get("INGEST_TOKEN") or None,
                   password_hash=os.environ.get("WEB_PASSWORD_HASH") or None,
                   session_secret=secret or secrets.token_hex(32),
                   command_ttl_s=int(os.environ.get("COMMAND_TTL_S", "60")),
                   cookie_secure=os.environ.get("COOKIE_INSECURE") != "1")


def normalize_db_url(url: str) -> str:
    """Neon/Render cho 'postgres://' hoặc 'postgresql://'; SQLAlchemy cần driver psycopg 3."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


# ---------------- mô hình request ----------------
class LoginBody(BaseModel):
    password: str = Field(max_length=200)


class CommandBody(BaseModel):
    kind: Literal["mode", "fan", "buzzer"]
    value: Literal["auto", "manual", "on", "off"]


class IngestBody(BaseModel):
    snapshot: dict | None = None
    history: list[dict] = Field(default_factory=list, max_length=2000)
    forecast: dict | None = None
    events: list[dict] = Field(default_factory=list, max_length=1000)
    acks: list[dict] = Field(default_factory=list, max_length=200)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "?")[:64]


def create_app(settings: Settings | None = None, *, clock=time.time) -> FastAPI:
    settings = settings or Settings.from_env()
    url = normalize_db_url(settings.database_url)
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    metadata.create_all(engine)
    dialect = engine.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as upsert_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as upsert_insert

    app = FastAPI(title="Trạm không khí AIoT", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.engine = engine
    login_limiter = RateLimiter(5, 60.0)
    command_limiter = RateLimiter(10, 60.0)
    purge_state = {"last": 0.0}
    lock = threading.Lock()

    def now() -> int:
        return int(clock())

    def logged_in(request: Request) -> bool:
        return check_session(settings.session_secret, request.cookies.get(COOKIE), now())

    def require_pi(request: Request):
        token = settings.ingest_token
        auth = request.headers.get("authorization", "")
        given = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not token or not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "sai INGEST_TOKEN")

    def expire_commands(conn, t: int):
        conn.execute(update(commands_t)
                     .where(commands_t.c.status.in_(("pending", "sent")),
                            commands_t.c.created_at < t - settings.command_ttl_s)
                     .values(status="expired", done_at=t,
                             detail="Pi không nhận lệnh kịp (mất kết nối?)"))

    def purge(conn, t: int):
        if t - purge_state["last"] < PURGE_EVERY_S:
            return
        purge_state["last"] = t
        conn.execute(delete(history_t).where(history_t.c.ts < t - HISTORY_KEEP_S))
        conn.execute(delete(forecasts_t).where(forecasts_t.c.made_at < t - HISTORY_KEEP_S))
        conn.execute(delete(commands_t).where(commands_t.c.created_at < t - COMMANDS_KEEP_S))
        cutoff = conn.execute(select(events_t.c.id).order_by(events_t.c.id.desc())
                              .offset(EVENTS_KEEP).limit(1)).scalar()
        if cutoff is not None:
            conn.execute(delete(events_t).where(events_t.c.id <= cutoff))

    # ---------------- Pi ----------------
    @app.post("/api/ingest", dependencies=[Depends(require_pi)])
    def ingest(body: IngestBody):
        t = now()
        with lock, engine.begin() as conn:
            if body.snapshot is not None:
                data = json.dumps(body.snapshot, ensure_ascii=False)[:20000]
                stmt = upsert_insert(snapshot_t).values(id=1, data=data, last_seen=t)
                conn.execute(stmt.on_conflict_do_update(index_elements=["id"],
                                                        set_={"data": data, "last_seen": t}))
            for row in body.history:
                ts = _int(row.get("ts"))
                if ts is None:
                    continue
                vals = {k: _num(row.get(k)) for k in ("pm1", "pm25", "pm10", "temp", "hum", "lux")}
                label = row.get("label") if isinstance(row.get("label"), str) else None
                vals["label"] = label[:20] if label else None
                stmt = upsert_insert(history_t).values(ts=ts, **vals)
                conn.execute(stmt.on_conflict_do_update(index_elements=["ts"], set_=vals))
            fc = body.forecast
            if isinstance(fc, dict) and _int(fc.get("made_at")) is not None \
                    and isinstance(fc.get("values"), list):
                made_at = fc["made_at"]
                model = str(fc.get("model") or "")[:40]
                conn.execute(delete(forecasts_t).where(forecasts_t.c.made_at == made_at))
                for h, v in enumerate(fc["values"][:24], start=1):
                    conn.execute(forecasts_t.insert().values(
                        made_at=made_at, horizon_h=h, pm25_pred=_num(v), model=model))
            for ev in body.events:
                pi_id = _int(ev.get("id"))
                ts = _int(ev.get("ts"))
                if pi_id is None or ts is None:
                    continue
                stmt = upsert_insert(events_t).values(
                    pi_id=pi_id, ts=ts, kind=str(ev.get("kind") or "")[:40],
                    detail=str(ev.get("detail") or "")[:500])
                conn.execute(stmt.on_conflict_do_nothing(index_elements=["pi_id"]))
            for ack in body.acks:
                cid = ack.get("id")
                status = ack.get("status")
                if not isinstance(cid, str) or status not in ("done", "rejected"):
                    continue
                conn.execute(update(commands_t)
                             .where(commands_t.c.id == cid,
                                    commands_t.c.status.in_(("pending", "sent")))
                             .values(status=status, done_at=t,
                                     detail=str(ack.get("detail") or "")[:300]))
            expire_commands(conn, t)
            rows = conn.execute(select(commands_t)
                                .where(commands_t.c.status.in_(("pending", "sent")))
                                .order_by(commands_t.c.created_at)).mappings().all()
            if rows:
                conn.execute(update(commands_t)
                             .where(commands_t.c.id.in_([r["id"] for r in rows]))
                             .values(status="sent"))
            purge(conn, t)
        return {"server_time": t,
                "commands": [{"id": r["id"], "kind": r["kind"], "value": r["value"],
                              "age_s": t - r["created_at"],
                              "source": f"web ip={r['ip']}"} for r in rows]}

    # ---------------- công khai ----------------
    @app.get("/api/status")
    def status(request: Request):
        t = now()
        with engine.connect() as conn:
            row = conn.execute(select(snapshot_t).where(snapshot_t.c.id == 1)).mappings().first()
        snap = json.loads(row["data"]) if row else None
        last_seen = row["last_seen"] if row else None
        return {"server_time": t, "last_seen": last_seen,
                "pi_online": last_seen is not None and t - last_seen <= PI_ONLINE_S,
                "logged_in": logged_in(request), "command_ttl_s": settings.command_ttl_s,
                "snapshot": snap}

    @app.get("/api/history")
    def history(hours: float = Query(6, gt=0, le=168)):
        since = now() - int(hours * 3600)
        with engine.connect() as conn:
            rows = conn.execute(select(history_t).where(history_t.c.ts >= since)
                                .order_by(history_t.c.ts)).mappings().all()
        return {"rows": [dict(r) for r in rows]}

    @app.get("/api/forecast")
    def forecast():
        with engine.connect() as conn:
            made_at = conn.execute(select(func.max(forecasts_t.c.made_at))).scalar()
            if made_at is None:
                return {"made_at": None, "model": None, "values": []}
            rows = conn.execute(select(forecasts_t).where(forecasts_t.c.made_at == made_at)
                                .order_by(forecasts_t.c.horizon_h)).mappings().all()
        return {"made_at": made_at, "model": rows[0]["model"] if rows else None,
                "values": [{"horizon_h": r["horizon_h"], "pm25": r["pm25_pred"]} for r in rows]}

    @app.get("/api/events")
    def events(limit: int = Query(30, ge=1, le=200)):
        with engine.connect() as conn:
            rows = conn.execute(select(events_t.c.ts, events_t.c.kind, events_t.c.detail)
                                .order_by(events_t.c.ts.desc(), events_t.c.id.desc())
                                .limit(limit)).mappings().all()
        return {"rows": [dict(r) for r in rows]}

    @app.get("/api/commands")
    def list_commands(limit: int = Query(10, ge=1, le=50)):
        t = now()
        with lock, engine.begin() as conn:
            expire_commands(conn, t)
            rows = conn.execute(select(commands_t.c.id, commands_t.c.kind, commands_t.c.value,
                                       commands_t.c.created_at, commands_t.c.status,
                                       commands_t.c.done_at, commands_t.c.detail)
                                .order_by(commands_t.c.created_at.desc()).limit(limit)
                                ).mappings().all()
        return {"rows": [dict(r) for r in rows]}

    # ---------------- đăng nhập và lệnh ----------------
    @app.post("/api/login")
    def login(request: Request, body: LoginBody):
        ip = client_ip(request)
        if not login_limiter.allow(ip):
            raise HTTPException(429, "Thử quá nhiều lần, đợi 1 phút")
        if not settings.password_hash:
            raise HTTPException(503, "Chưa đặt WEB_PASSWORD_HASH trên server")
        if not verify_password(body.password, settings.password_hash):
            log.info("Đăng nhập sai từ %s", ip)
            raise HTTPException(401, "Sai mật khẩu")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, make_session(settings.session_secret, SESSION_TTL_S, now()),
                        max_age=SESSION_TTL_S, httponly=True, secure=settings.cookie_secure,
                        samesite="strict", path="/")
        log.info("Đăng nhập từ %s", ip)
        return resp

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.post("/api/command")
    def command(request: Request, body: CommandBody):
        if not logged_in(request):
            raise HTTPException(401, "Cần đăng nhập")
        # Header tuỳ chỉnh: form ở trang khác không gửi được (chống CSRF, cùng SameSite=Strict)
        if request.headers.get("x-requested-with") != "aiot":
            raise HTTPException(403, "Thiếu header X-Requested-With")
        if body.value not in VALID_VALUES[body.kind]:
            raise HTTPException(422, f"Giá trị không hợp lệ cho {body.kind}")
        ip = client_ip(request)
        if not command_limiter.allow(ip):
            raise HTTPException(429, "Quá 10 lệnh mỗi phút, đợi một chút")
        t = now()
        cid = uuid.uuid4().hex
        with lock, engine.begin() as conn:
            conn.execute(commands_t.insert().values(id=cid, kind=body.kind, value=body.value,
                                                    ip=ip, created_at=t, status="pending"))
            row = conn.execute(select(snapshot_t.c.last_seen)).scalar()
        log.info("Lệnh %s %s từ %s", body.kind, body.value, ip)
        online = row is not None and t - row <= PI_ONLINE_S
        return {"id": cid, "status": "pending", "pi_online": online,
                "note": None if online else
                f"Pi đang mất kết nối, lệnh sẽ hết hạn sau {settings.command_ttl_s} s nếu Pi không nhận."}

    # ---------------- trang ----------------
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _lazy_app():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


if os.environ.get("AIOT_CLOUD_NO_APP") != "1":
    app = _lazy_app()
