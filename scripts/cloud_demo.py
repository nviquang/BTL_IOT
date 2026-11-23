"""
Chạy thử web trên cloud ngay trên laptop, không cần Pi, Render hay Neon.

- Tạo DB SQLite giả lập của trạm (6 giờ số liệu, nhãn AI, dự báo) ở thư mục tạm.
- Chạy server cloud (uvicorn) ở http://127.0.0.1:8000 với DB SQLite cục bộ.
- Chạy aiot.uplink + aiot.controller (relay và còi giả, chỉ in ra màn hình) trong cùng tiến
  trình, nên bấm nút trên web sẽ thấy quạt/còi giả đổi trạng thái sau vài giây.

Chạy: .venv/Scripts/python scripts/cloud_demo.py [--password <mật khẩu thử>] [--port 8000]
Mật khẩu thử mặc định: demo-local-123 (chỉ dùng cục bộ, không dùng cho Render).
"""
import argparse
import dataclasses
import json
import math
import os
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["AIOT_CLOUD_NO_APP"] = "1"


def seed(conn, now):
    from aiot.db import RawSample, insert_prediction, insert_raw, log_event
    rnd = random.Random(1)
    labels = []
    for ts in range(now - 6 * 3600, now, 10):
        h = (ts - (now - 6 * 3600)) / 3600
        pm = 18 + 8 * math.sin(h) + rnd.uniform(-2, 2)
        label = "normal"
        if 2.0 < h < 2.4:
            pm += 70; label = "cooking"
        if 4.5 < h < 4.7:
            pm += 250; label = "smoking"
        insert_raw(conn, RawSample(ts, pm * 0.7, pm, pm * 1.3, 27 + math.sin(h), 72.0, 300.0, True))
        if (ts - now) % 30 == 0:
            p = {"normal": 0.85, "cooking": 0.05, "pollution": 0.05, "smoking": 0.05}
            if label != "normal":
                p = {k: 0.05 for k in p}; p[label] = 0.85
            insert_prediction(conn, ts, label, label, json.dumps(p), "rf-demo", commit=False)
        labels.append(label)
    for i, v in enumerate([24, 27, 31, 36, 40, 38], start=1):
        conn.execute("INSERT INTO forecasts (made_at, horizon_h, pm25_pred, model) VALUES (?,?,?,?)",
                     (now - 600, i, v, "lstm"))
    log_event(conn, now - 2 * 3600, "source_change", "cooking p=0.85", commit=False)
    log_event(conn, now - 2 * 3600, "fan_on", "AI: nấu ăn", commit=False)
    log_event(conn, now - 5400, "fan_off", "AI: bình thường", commit=False)
    conn.commit()


def fake_station(db_path, cfg, stop):
    """Collector + controller giả: thêm mẫu 10 giây/lần, chạy step mỗi 5 giây."""
    from aiot.controller import step
    from aiot.db import RawSample, connect, insert_raw, set_state

    class Relay:
        on = None

        def set(self, on):
            if on != self.on:
                print(f"[Pi giả] quạt {'BẬT' if on else 'TẮT'}", flush=True)
            self.on = on

    class Buzzer:
        def burst(self, a, b, d):
            print(f"[Pi giả] còi KÊU {d:.0f} giây", flush=True)

        def silence(self):
            pass

    conn = connect(db_path)
    relay, buzzer = Relay(), Buzzer()
    rnd = random.Random(2)
    while not stop.is_set():
        now = int(time.time())
        insert_raw(conn, RawSample(now - now % 10, 14.0, 20 + rnd.uniform(-2, 2), 26.0, 27.5,
                                   71.0, 320.0, True))
        set_state(conn, "source_state", json.dumps({"label": "normal", "since": now - 1800,
                                                    "last_ts": now, "history": []}))
        from aiot.db import insert_prediction
        insert_prediction(conn, now, "normal", "normal", json.dumps(
            {"normal": 0.82, "cooking": 0.1, "pollution": 0.05, "smoking": 0.03}), "rf-demo")
        try:
            step(conn, relay, cfg.control, now, monotonic=lambda: 10_000, buzzer=buzzer,
                 bcfg=cfg.buzzer, tcfg=cfg.telegram)
        except Exception as e:
            print("[Pi giả] lỗi controller:", e, flush=True)
        stop.wait(5)


def main():
    for stream in (sys.stdout, sys.stderr):      # console Windows cp1252 không in được tiếng Việt
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--password", default="demo-local-123")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    import uvicorn
    from aiot import uplink
    from aiot.config import CloudCfg, Config, ControlCfg, Secrets
    from aiot.db import connect, init_schema
    from cloud.app import Settings, create_app
    from cloud.security import hash_password

    tmp = Path(tempfile.mkdtemp(prefix="aiot_cloud_demo_"))
    token = "demo-token"
    db_path = tmp / "pi.db"
    conn = connect(db_path)
    init_schema(conn)
    seed(conn, int(time.time()))

    settings = Settings(database_url=f"sqlite:///{tmp / 'cloud.db'}", ingest_token=token,
                        password_hash=hash_password(args.password), cookie_secure=False)
    app = create_app(settings)
    cfg = dataclasses.replace(Config(), db_path=str(db_path),
                              control=ControlCfg(loop_s=5),
                              cloud=CloudCfg(enabled=True, url=f"http://127.0.0.1:{args.port}"),
                              secrets=Secrets(ingest_token=token))
    stop = threading.Event()
    threading.Thread(target=fake_station, args=(db_path, cfg, stop), daemon=True).start()

    def run_uplink():
        time.sleep(2)
        uplink.run(uplink.Uplink(connect(db_path), cfg), stop)
    threading.Thread(target=run_uplink, daemon=True).start()
    print(f"Mở http://127.0.0.1:{args.port}  (dữ liệu thử ở {tmp})", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        stop.set()


if __name__ == "__main__":
    main()
