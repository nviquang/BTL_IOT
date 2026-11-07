"""
Tính các chỉ số nghiệm thu từ DB SQLite của trạm (xem docs/nghiem_thu.md).

Chỉ đọc DB: mở chế độ read-only (mode=ro), không tạo bảng; file --db không tồn tại thì báo
lỗi và thoát mã 2 (không tạo DB rỗng khi gõ sai đường dẫn). Chạy được trên Pi:
chỉ dùng thư viện chuẩn, numpy và gói aiot.

Định nghĩa:
- valid_ratio: số cửa sổ 5 phút valid=1 trong [start, end) chia cho số cửa sổ KỲ VỌNG
  (end - start) // 300, nên lúc Pi tắt cũng bị tính là thiếu.
- control_reaction_times: với mỗi sự kiện fan_on lý do 'enter' hoặc 'danger', số giây từ mẫu
  thô (raw_readings) ĐẦU TIÊN của chuỗi mẫu PM2.5 > enter_pm25 liên tục dẫn tới lúc bật, tới
  sự kiện fan_on. Chuỗi bị ngắt bởi một mẫu <= ngưỡng hoặc khoảng mất dữ liệu > gap_s.
  Bỏ qua lần bật vì dự báo ('proactive'), ghi đè thủ công, và lần bật không còn mẫu thô
  (collector chỉ giữ mẫu thô 7 ngày). Với luật mặc định, chuỗi bước từ thấp lên cao kỳ vọng
  phản ứng trong khoảng enter_mean_s + enter_hold_s + loop_s; mức nguy hại trong
  danger_window_s + loop_s.
- count_flaps: số lần nhảy trạng thái sai luật aiot.control.decide: BẢO VỆ giữ chưa tới
  min_state_s (fan_on -> fan_off), hoặc vào lại vì 'enter' chưa tới enter_hold_s sau fan_off.
  Trạng thái trước lần đổi đầu tiên lấy từ sự kiện quạt trước start. quick_reentries đếm riêng
  các lần vào lại < min_state_s sau khi thoát (đúng luật, chỉ để tham khảo).
- report_punctuality: một ngày (giờ địa phương) đúng giờ khi có tin 'morning_report' tạo trong
  ngày đó và đã gửi (sent_at) trong khoảng [giờ báo cáo, giờ báo cáo + tolerance_s]. Việc gộp
  tin cũ (collapse_stale_alerts) không bao giờ đụng 'morning_report', nên sent_at là giờ gửi
  thật. Ngày không có tin hoặc tin chưa gửi là không đúng giờ.
- fan_effect: chia mỗi cửa sổ 5 phút thành "bật" (bật cả 300 s) hoặc "tắt" (0 s) theo nhật ký
  sự kiện (aiot.report.fan_on_seconds); cửa sổ lẫn lộn bị bỏ. So PM2.5 trung bình và độ dốc
  (µg/m³ mỗi giờ, từ hai cửa sổ liền nhau cùng trạng thái). Trung bình bị nhiễu vì quạt bật
  đúng lúc không khí xấu; độ dốc mới cho thấy quạt có làm PM2.5 giảm nhanh hơn không.
- station_correlation: ghép theo giờ, hệ số tương quan Pearson và hồi quy tuyến tính
  công khai ≈ slope · trạm + intercept; MAE trước và sau hiệu chỉnh.

Ví dụ (trên Pi):
  cd ~/BTL_IOT && venv/bin/python scripts/acceptance_report.py \\
      --start "2026-11-10" --end "2026-11-17" [--public-csv tram.csv]
CSV trạm công khai: có cột timestamp (giờ địa phương, theo giờ) và pm25 (µg/m³), như
training/export_hourly.py hoặc training/fetch_public.py ghi ra.
"""
import argparse
import csv
import math
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WINDOW_S = 300
HOUR_S = 3600
WINDOWS_PER_HOUR = HOUR_S // WINDOW_S
REACTION_KINDS = ("enter", "danger")


# ---------------- ổn định ----------------
def valid_ratio(conn, start, end, window_s=WINDOW_S) -> float:
    """Tỉ lệ cửa sổ hợp lệ so với số cửa sổ kỳ vọng trong [start, end)."""
    expected = (int(end) - int(start)) // window_s
    if expected <= 0:
        raise ValueError("khoảng thời gian ngắn hơn một cửa sổ 5 phút")
    got = conn.execute("SELECT count(*) FROM readings WHERE valid = 1 AND ts >= ? AND ts < ?",
                       (int(start), int(end))).fetchone()[0]
    return min(got / expected, 1.0)


# ---------------- điều khiển ----------------
def control_reaction_times(conn, start, end, enter_pm25=55.0, gap_s=120) -> list[float]:
    """Thời gian phản ứng (giây) của từng lần bật quạt theo số đo, xem docstring module."""
    events = conn.execute(
        "SELECT ts, detail FROM events WHERE kind = 'fan_on' AND ts >= ? AND ts < ? "
        "ORDER BY ts, rowid", (int(start), int(end))).fetchall()
    out = []
    for ev_ts, detail in events:
        if (detail or "").split(" ")[0] not in REACTION_KINDS:
            continue
        rows = conn.execute(
            "SELECT ts, pm25 FROM raw_readings WHERE ts <= ? AND pm25 IS NOT NULL "
            "ORDER BY ts DESC", (int(ev_ts),))
        first, prev = None, int(ev_ts)
        for ts, pm in rows:
            if prev - ts > gap_s or pm <= enter_pm25:
                break
            first, prev = ts, ts
        if first is not None:
            out.append(float(ev_ts - first))
    return out


def _fan_transitions(conn, start, end):
    """[(trước, sau)] mỗi lần đổi trạng thái quạt có ts trong [start, end); mỗi phần tử là
    (ts, kind, detail). Đọc cả sự kiện trước start để biết trạng thái trước lần đổi đầu tiên;
    sự kiện trùng loại liên tiếp không phải lần đổi."""
    rows = conn.execute(
        "SELECT ts, kind, detail FROM events WHERE kind IN ('fan_on', 'fan_off') AND ts < ? "
        "ORDER BY ts, rowid", (int(end),)).fetchall()
    out, prev = [], None
    for row in rows:
        if prev is not None and row[1] == prev[1]:
            continue
        if prev is not None and row[0] >= start:
            out.append((prev, row))
        prev = row
    return out


def count_flaps(conn, start, end, min_state_s=600, enter_hold_s=300) -> int:
    """Số lần nhảy trạng thái SAI LUẬT trong [start, end):
    - BẢO VỆ giữ chưa tới min_state_s (fan_on rồi fan_off sớm hơn), hoặc
    - vào lại BẢO VỆ vì 'enter' chưa tới enter_hold_s sau khi thoát.
    Vào lại vì 'danger'/'proactive' ngay sau khi thoát là đúng luật (xem quick_reentries)."""
    flaps = 0
    for (p_ts, p_kind, _), (ts, kind, detail) in _fan_transitions(conn, start, end):
        gap = ts - p_ts
        if p_kind == "fan_on" and gap < min_state_s:
            flaps += 1
        elif kind == "fan_on" and (detail or "").split(" ")[0] == "enter" and gap < enter_hold_s:
            flaps += 1
    return flaps


def quick_reentries(conn, start, end, min_state_s=600) -> int:
    """Số lần vào lại BẢO VỆ chưa tới min_state_s sau khi thoát (chỉ để tham khảo: luật cho
    phép vào lại sau enter_hold_s, hoặc ngay lập tức khi nguy hại/dự báo cao)."""
    return sum(1 for (p_ts, p_kind, _), (ts, kind, _) in _fan_transitions(conn, start, end)
               if p_kind == "fan_off" and ts - p_ts < min_state_s)


# ---------------- Telegram ----------------
def _scheduled(day: date, report_time: str) -> int:
    hh, mm = (int(p) for p in report_time.split(":"))
    return int(datetime(day.year, day.month, day.day, hh, mm).timestamp())


def morning_report_details(conn, days, report_time="06:30", tolerance_s=900):
    """[(ngày, created_at, sent_at, đúng giờ?)] mỗi ngày; created_at/sent_at None nếu không có.
    Nhiều tin trong một ngày: lấy tin gửi sớm nhất (tin chưa gửi xếp sau)."""
    rows = conn.execute("SELECT created_at, sent_at FROM outbox WHERE kind = 'morning_report' "
                        "ORDER BY created_at, id").fetchall()
    by_day = {}
    for created, sent in rows:
        d = datetime.fromtimestamp(created).date()
        best = by_day.get(d)
        key = (sent is None, sent or 0)
        if best is None or key < (best[1] is None, best[1] or 0):
            by_day[d] = (created, sent)
    out = []
    for d in days:
        created, sent = by_day.get(d, (None, None))
        due = _scheduled(d, report_time)
        on_time = sent is not None and due <= sent <= due + tolerance_s
        out.append((d, created, sent, on_time))
    return out


def report_punctuality(conn, days, report_time="06:30", tolerance_s=900) -> tuple[int, int]:
    """(số ngày gửi đúng giờ, số ngày). days: các ngày địa phương (datetime.date)."""
    details = morning_report_details(conn, days, report_time, tolerance_s)
    return sum(1 for *_, ok in details if ok), len(details)


# ---------------- hiệu quả lọc ----------------
def fan_effect(conn, start, end) -> dict:
    """So PM2.5 khi quạt bật và tắt, xem docstring module."""
    from aiot.report import fan_on_seconds
    rows = conn.execute(
        "SELECT ts, pm25 FROM readings WHERE valid = 1 AND pm25 IS NOT NULL "
        "AND ts >= ? AND ts < ? ORDER BY ts", (int(start), int(end))).fetchall()
    states = []                                     # (ts, pm25, 'on'|'off'|None)
    for ts, pm in rows:
        on_s = fan_on_seconds(conn, ts, ts + WINDOW_S)
        state = "on" if on_s >= WINDOW_S else "off" if on_s == 0 else None
        states.append((int(ts), float(pm), state))
    out = {}
    for name in ("on", "off"):
        vals = [pm for _, pm, s in states if s == name]
        slopes = [(b[1] - a[1]) * HOUR_S / WINDOW_S
                  for a, b in zip(states, states[1:])
                  if a[2] == b[2] == name and b[0] - a[0] == WINDOW_S]
        out[f"{name}_windows"] = len(vals)
        out[f"{name}_mean"] = sum(vals) / len(vals) if vals else None
        out[f"{name}_slope_per_h"] = sum(slopes) / len(slopes) if slopes else None
    return out


# ---------------- cảm biến ----------------
def local_hourly(conn, start, end, min_windows=WINDOWS_PER_HOUR // 2):
    """[(đầu giờ, PM2.5 TB)] từ readings valid=1; giờ có ít hơn min_windows cửa sổ bị bỏ."""
    rows = conn.execute(
        "SELECT ts / 3600 * 3600 AS h, avg(pm25), count(*) FROM readings "
        "WHERE valid = 1 AND pm25 IS NOT NULL AND ts >= ? AND ts < ? GROUP BY h ORDER BY h",
        (int(start), int(end))).fetchall()
    return [(int(h), float(m)) for h, m, n in rows if n >= min_windows]


def load_public_csv(path) -> list[tuple[int, float]]:
    """[(ts, pm25)] từ CSV có cột timestamp (giờ địa phương hoặc có múi giờ) và pm25."""
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            v = (row.get("pm25") or "").strip()
            if not v:
                continue
            ts = datetime.fromisoformat(row["timestamp"].strip()).timestamp()
            out.append((int(ts) // HOUR_S * HOUR_S, float(v)))
    return out


def station_correlation(local, public) -> dict:
    """Tương quan và hiệu chỉnh tuyến tính giữa hai chuỗi [(ts, giá trị)], ghép theo ts."""
    import numpy as np
    pub = dict(public)
    pairs = [(v, pub[ts]) for ts, v in local if ts in pub]
    if len(pairs) < 3:
        raise ValueError(f"cần ít nhất 3 giờ chung, có {len(pairs)}")
    x = np.array([p[0] for p in pairs], dtype=float)
    y = np.array([p[1] for p in pairs], dtype=float)
    if x.std() == 0 or y.std() == 0:
        raise ValueError("một trong hai chuỗi không đổi, không tính được tương quan")
    slope, intercept = np.polyfit(x, y, 1)
    fit = slope * x + intercept
    r = float(np.corrcoef(x, y)[0, 1])
    return {
        "n": len(pairs), "r": r, "slope": float(slope), "intercept": float(intercept),
        "mae_raw": float(np.mean(np.abs(y - x))),
        "mae_fit": float(np.mean(np.abs(y - fit))),
        "rmse_fit": float(np.sqrt(np.mean((y - fit) ** 2))),
    }


# ---------------- CLI ----------------
def _parse_time(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp())


def _num(v, digits=1, suffix=""):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "không có dữ liệu"
    return f"{v:.{digits}f}".replace(".", ",") + suffix


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Chỉ số nghiệm thu trạm không khí")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--db", help="mặc định db_path trong config.yaml")
    parser.add_argument("--start", help="giờ địa phương, ví dụ 2026-11-10 hoặc "
                                        "'2026-11-10 06:00'; mặc định 7 ngày trước --end")
    parser.add_argument("--end", help="mặc định bây giờ")
    parser.add_argument("--public-csv", help="CSV trạm công khai (timestamp, pm25) theo giờ")
    parser.add_argument("--tolerance-min", type=int, default=15,
                        help="báo cáo sáng gửi muộn tối đa bao nhiêu phút vẫn tính đúng giờ")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):        # console Windows mặc định cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    from aiot.config import load_config
    cfg = load_config(args.config, ROOT / ".env")
    ctl = cfg.control
    end = _parse_time(args.end) if args.end else int(time.time())
    start = _parse_time(args.start) if args.start else end - 7 * 86400
    if end <= start:
        parser.error("--end phải sau --start")

    db_path = Path(args.db or cfg.db_path)
    if not db_path.is_file():
        print(f"Lỗi: không có file DB {db_path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        fmt = "%Y-%m-%d %H:%M"
        print(f"Khoảng: {datetime.fromtimestamp(start).strftime(fmt)} -> "
              f"{datetime.fromtimestamp(end).strftime(fmt)}")

        print("\n[Ổn định]")
        print(f"Cửa sổ 5 phút hợp lệ: {_num(valid_ratio(conn, start, end) * 100, 1, '%')} "
              f"(tiêu chí >= 95%)")

        print("\n[Điều khiển]")
        times = control_reaction_times(conn, start, end, ctl.enter_pm25, ctl.stale_s)
        bound = ctl.enter_mean_s + ctl.enter_hold_s + ctl.loop_s
        print(f"Số lần bật theo số đo có mẫu thô: {len(times)}")
        if times:
            print("Thời gian phản ứng (s): " + ", ".join(f"{t:.0f}" for t in times))
            print(f"Lâu nhất {max(times):.0f} s; kỳ vọng <= {bound} s "
                  f"(enter_mean_s + enter_hold_s + loop_s)")
        flaps = count_flaps(conn, start, end, ctl.min_state_s, ctl.enter_hold_s)
        print(f"Số lần nhảy trạng thái sai luật (BẢO VỆ < {ctl.min_state_s} s, hoặc vào lại "
              f"vì vượt ngưỡng < {ctl.enter_hold_s} s sau khi thoát): {flaps} (tiêu chí 0)")
        print(f"Tham khảo: vào lại BẢO VỆ < {ctl.min_state_s} s sau khi thoát: "
              f"{quick_reentries(conn, start, end, ctl.min_state_s)} lần (đúng luật)")

        print("\n[Hiệu quả lọc]")
        fx = fan_effect(conn, start, end)
        for name, label in (("on", "Quạt bật"), ("off", "Quạt tắt")):
            print(f"{label}: {fx[f'{name}_windows']} cửa sổ, PM2.5 TB "
                  f"{_num(fx[f'{name}_mean'])} µg/m³, độ dốc "
                  f"{_num(fx[f'{name}_slope_per_h'], 2)} µg/m³/giờ")

        print("\n[Telegram] Báo cáo sáng")
        first = datetime.fromtimestamp(start).date()
        last = datetime.fromtimestamp(end - 1).date()
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        tol = args.tolerance_min * 60
        for d, created, sent, ok in morning_report_details(conn, days, cfg.report.time, tol):
            verdict = "đúng giờ" if ok else "KHÔNG đúng giờ"
            if created is None:
                print(f"  {d.isoformat()}: không có tin, {verdict}")
                continue
            c = datetime.fromtimestamp(created).strftime("%H:%M")
            s = datetime.fromtimestamp(sent).strftime("%d/%m %H:%M") if sent else "chưa gửi"
            print(f"  {d.isoformat()}: tạo {c}, gửi {s}, {verdict}")
        on_time, total = report_punctuality(conn, days, cfg.report.time, tol)
        print(f"Đúng giờ {on_time}/{total} (tiêu chí >= 6/7, trễ tối đa {args.tolerance_min} phút)")

        print("\n[Cảm biến]")
        hum = conn.execute("SELECT avg(hum), max(hum) FROM readings WHERE valid = 1 "
                           "AND ts >= ? AND ts < ?", (start, end)).fetchone()
        print(f"Độ ẩm TB {_num(hum[0])} %, cao nhất {_num(hum[1])} %")
        if args.public_csv:
            try:
                st = station_correlation(local_hourly(conn, start, end),
                                         load_public_csv(args.public_csv))
            except ValueError as e:
                print(f"Không so được với trạm công khai: {e}")
            else:
                print(f"So với trạm công khai: {st['n']} giờ chung, r = {_num(st['r'], 3)}")
                print(f"Hiệu chỉnh: công khai ≈ {_num(st['slope'], 3)} · trạm + "
                      f"{_num(st['intercept'], 2)}")
                print(f"MAE trước {_num(st['mae_raw'], 2)}, sau {_num(st['mae_fit'], 2)} µg/m³; "
                      f"RMSE sau {_num(st['rmse_fit'], 2)} µg/m³")
        else:
            print("Bỏ qua so sánh trạm công khai (thêm --public-csv)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
