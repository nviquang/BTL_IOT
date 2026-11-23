"""
Bộ mô phỏng dữ liệu 1 giây cho bộ nhận diện nguồn bụi (4 nhãn).

Mỗi "phiên" (session) dài 40–100 phút = nền + 0–2 sự kiện trong nhà, nhãn theo từng giây:
  normal     nền sạch (PM2.5 < 55,5), có nhiễu: tắm (ẩm tăng), bật/tắt đèn, gai 1 khung
  pollution  nền >= 55,5 µg/m³ (ô nhiễm ngoài trời tràn vào), kể cả lúc nền đang tăng dần
  cooking    nấu ăn: tăng dần vài phút, giữ, tắt chậm; ẩm và nhiệt tăng
  smoking    hút thuốc / đốt nhang: các "hơi" nhọn, tan nhanh
Ưu tiên khi chồng nhau: smoking > cooking > pollution > normal.

Tham số lấy từ số liệu thật trên Pi (training/data/raw_readings.csv, readings.csv):
- nền: PM1/PM2.5 ≈ 0,69 (sạch, 07–09/11) và 0,61 (nền cao 23/11), PM10/PM2.5 ≈ 1,18;
  nhiễu tương đối ~1 % mỗi 10 giây; đèn phòng 220 / 590 / 1030 / 1120 lux.
- đốt nhang 21:07 ngày 23/11: phần vượt nền có PM1/PM2.5 0,27–0,42, PM10/PM2.5 1,4–2,0,
  hai đỉnh 805 và 1061 µg/m³ cách nhau ~50 giây, giảm 75 % trong ~20 giây.
- nấu ăn: CHƯA có số liệu thật; tỉ lệ hạt, mức tăng ẩm/nhiệt là giả định, phải chỉnh lại
  khi có sự kiện có nhãn (xem docs/nhan_dien_nguon_bui.md).

Chỉ dùng numpy để scripts/inject_pm.py chạy được trên Pi.
Chạy: .venv/Scripts/python -m training.simulate_sources --sessions 400 --seed 42
"""
import argparse
import json
from pathlib import Path

import numpy as np

LABELS = ("normal", "cooking", "pollution", "smoking")
CODE = {name: i for i, name in enumerate(LABELS)}
POLLUTION_PM25 = 55.5            # ranh giới nhóm AQI "Xấu" (US EPA 2024)
WARMUP_S = 600                   # 10 phút đầu chỉ có nền (bộ đệm đặc trưng có lịch sử)
LUX_LEVELS = (40.0, 220.0, 590.0, 1030.0, 1120.0)

DATA = Path(__file__).resolve().parent / "data"


# ---------------- nền ----------------
def _ou(rng, n, sigma, tau):
    """Quá trình Ornstein-Uhlenbeck rời rạc 1 giây (trung bình 0)."""
    x = np.zeros(n)
    a = np.exp(-1.0 / tau)
    s = sigma * np.sqrt(1 - a * a)
    eps = rng.normal(0, s, n)
    for i in range(1, n):
        x[i] = a * x[i - 1] + eps[i]
    return x


def background(rng, n, mode):
    """Mức nền PM2.5 (µg/m³) theo giây và meta."""
    drift = np.exp(_ou(rng, n, 0.12, 1800.0))
    if mode == "clean":
        lvl = np.full(n, np.exp(rng.uniform(np.log(3), np.log(42))))
    elif mode == "high":
        lvl = np.full(n, rng.uniform(65, 250))
    else:                                       # onset (tăng dần) / clearing (giảm dần)
        lo, hi = rng.uniform(5, 40), rng.uniform(80, 250)
        center = rng.uniform(0.35, 0.8) * n
        width = rng.uniform(15, 60) * 60 / 4.0
        s = 1 / (1 + np.exp(-(np.arange(n) - center) / width))
        if mode == "clearing":
            s = 1 - s
        lvl = lo + (hi - lo) * s
    return lvl * drift


# ---------------- sự kiện ----------------
def _first_order(emission, tau):
    """Nồng độ phòng với nguồn phát emission (µg/m³/s) và thời gian thải tau (giây)."""
    c = np.zeros_like(emission)
    a = np.exp(-1.0 / tau)
    for i in range(1, emission.size):
        c[i] = a * c[i - 1] + emission[i]
    return c


def smoking_event(rng, n, start):
    """Phần vượt nền PM2.5 của hút thuốc/đốt nhang và khoảng có nhãn."""
    kind = rng.choice(["cigarette", "incense_short", "incense_long"], p=[0.45, 0.3, 0.25])
    if kind == "cigarette":
        dur, gap, amp = rng.uniform(240, 480), (20, 60), (60, 700)
    elif kind == "incense_short":               # như 21:07 ngày 23/11: 2–4 làn khói lớn
        dur, gap, amp = rng.uniform(40, 150), (25, 60), (250, 1000)
    else:                                       # que nhang cháy lâu: khói lăn tăn liên tục
        dur, gap, amp = rng.uniform(300, 1200), (4, 20), (20, 250)
    impulses = np.zeros(n)
    t = start
    while t < min(start + dur, n - 1):
        impulses[int(t)] += np.exp(rng.uniform(np.log(amp[0]), np.log(amp[1])))
        t += rng.uniform(*gap)
    # Làn khói tới cảm biến trong 3–10 giây, tan với tau 8–30 giây; ~8 % ở lại phòng lâu hơn
    rise = rng.uniform(3, 10)
    k_rise = np.exp(-np.arange(int(rise * 4)) / rise)
    k_rise /= k_rise.sum()
    arrive = np.convolve(impulses, k_rise)[:n]
    fast = _first_order(arrive, rng.uniform(8, 30))
    slow = _first_order(arrive * rng.uniform(0.03, 0.12), rng.uniform(300, 900))
    exc = fast + slow
    last = int(min(start + dur, n - 1))
    peak = exc[start:last + 1].max() if last > start else 0.0
    end = last
    while end < n - 1 and exc[end] > max(10.0, 0.12 * peak):
        end += 1
    ratios = (rng.uniform(0.25, 0.45), rng.uniform(1.4, 2.0))
    return exc, (int(start), int(end)), ratios, {"kind": kind, "dur_s": float(dur)}


def cooking_event(rng, n, start):
    """Phần vượt nền PM2.5, tăng ẩm, tăng nhiệt, ánh sáng của nấu ăn (tham số giả định)."""
    ramp = rng.uniform(60, 300)
    dur = rng.uniform(5, 30) * 60
    i = np.arange(n) - start
    env = np.clip(i / ramp, 0, 1) * (i <= dur)
    noise = np.exp(_ou(rng, n, 0.3, 30.0))
    bursts = np.zeros(n)
    for _ in range(rng.poisson(dur / 60)):            # lật/xào: bùng 20–60 giây
        b0 = int(start + rng.uniform(0, dur))
        bursts[b0:b0 + int(rng.uniform(20, 60))] += rng.uniform(0.5, 2.0)
    emission = env * noise * (1 + bursts)
    tau = rng.uniform(4, 15) * 60
    exc = _first_order(emission, tau)
    target = np.exp(rng.uniform(np.log(25), np.log(400)))
    exc *= target / max(exc.max(), 1e-9)
    stop = int(min(start + dur, n - 1))
    peak = exc.max()
    end = stop
    while end < n - 1 and exc[end] > max(8.0, 0.2 * peak):
        end += 1
    steam = _first_order(env * (1 + 0.3 * noise), rng.uniform(120, 300))
    steam /= max(steam.max(), 1e-9)
    dh = steam * rng.uniform(3, 15)
    dtemp = steam * rng.uniform(0.3, 2.0)
    dlux = env * (rng.uniform(100, 500) if rng.random() < 0.5 else 0.0)
    ratios = (rng.uniform(0.5, 0.75), rng.uniform(1.15, 1.5))
    return exc, (int(start), int(end)), ratios, dh, dtemp, dlux, {"dur_s": float(dur), "peak": float(peak)}


# ---------------- một phiên ----------------
def simulate_session(rng, duration_s=None, bg_mode=None, events=None):
    """Sinh một phiên. Trả (dict mảng 1 giây gồm 'label' mã số, meta)."""
    n = int(duration_s or rng.uniform(40, 100) * 60)
    bg_mode = bg_mode or rng.choice(["clean", "high", "onset", "clearing"], p=[0.5, 0.2, 0.18, 0.12])
    bg = background(rng, n, bg_mode)
    hi = np.clip((np.log(bg) - np.log(30)) / (np.log(150) - np.log(30)), 0, 1)
    r1_bg = (0.69 - 0.08 * hi) * (1 + rng.normal(0, 0.01))
    r10_bg = rng.normal(1.185, 0.02)
    pm25, pm1, pm10 = bg.copy(), bg * r1_bg, bg * r10_bg

    hum = rng.uniform(55, 85) + _ou(rng, n, 1.0, 1200.0)
    temp = rng.uniform(22, 32) + _ou(rng, n, 0.3, 1800.0)
    lux = np.full(n, rng.choice(LUX_LEVELS))
    for _ in range(rng.poisson(n / 1200)):            # bật/tắt đèn
        t0 = int(rng.uniform(0, n))
        lux[t0:] = rng.choice(LUX_LEVELS)

    label = np.where(bg >= POLLUTION_PM25, CODE["pollution"], CODE["normal"]).astype(np.int8)
    meta = {"n": n, "bg_mode": str(bg_mode), "events": []}

    if events is None:
        k = rng.choice([0, 1, 2], p=[0.25, 0.5, 0.25])
        events = [str(rng.choice(["smoking", "cooking"], p=[0.55, 0.45])) for _ in range(k)]
    t = WARMUP_S + rng.uniform(0, 600)
    local = []
    for ev in events:
        if t >= n - 300:
            break
        start = int(t)
        if ev == "smoking":
            exc, span, (er1, er10), info = smoking_event(rng, n, start)
        else:
            exc, span, (er1, er10), dh, dtemp, dlux, info = cooking_event(rng, n, start)
            hum, temp, lux = hum + dh, temp + dtemp, lux + dlux
        pm25, pm1, pm10 = pm25 + exc, pm1 + exc * er1, pm10 + exc * er10
        local.append((ev, span))
        meta["events"].append({"label": ev, "start": span[0], "end": span[1], **info})
        t = span[1] + rng.uniform(300, 1200)
    for ev in ("cooking", "smoking"):                 # smoking ghi sau cùng: ưu tiên cao nhất
        for name, (s0, s1) in local:
            if name == ev:
                label[s0:s1 + 1] = CODE[ev]

    # Nhiễu cho nhãn normal/pollution: tắm (ẩm tăng, không có bụi)
    if rng.random() < 0.25:
        t0 = int(rng.uniform(WARMUP_S, n))
        bump = np.zeros(n)
        bump[t0:t0 + int(rng.uniform(300, 900))] = 1.0
        hum = hum + _first_order(bump, 600.0) / 600.0 * rng.uniform(8, 20)

    return sensor_model(rng, pm1, pm25, pm10, temp, hum, lux) | {"label": label}, meta


# ---------------- mô hình cảm biến ----------------
def sensor_model(rng, pm1, pm25, pm10, temp, hum, lux):
    """Thêm độ trễ, nhiễu, làm tròn và mất mẫu như PMS7003 / DHT11 / BH1750 thật."""
    n = pm25.size

    def pms(x):
        y = _first_order(x * 0.45, 1 / -np.log(0.55))   # làm mượt nội bộ ~2 giây
        y *= 1 + rng.normal(0, 0.007, n)
        y += rng.normal(0, 1, n) * 0.4 * np.sqrt(np.maximum(y, 0))
        return np.round(np.clip(y, 0, 1000))

    p25, p1, p10 = pms(pm25), pms(pm1), pms(pm10)
    spikes = rng.random(n) < 2e-4                       # khung lỗi đơn lẻ
    k = rng.uniform(2, 4, spikes.sum())
    for arr in (p1, p25, p10):
        arr[spikes] = np.minimum(arr[spikes] * k, 1000)
    p1 = np.minimum(p1, p25)
    p10 = np.maximum(p10, p25)

    coarse, fine = np.maximum(p10 - p25, 0), np.maximum(p25 - p1, 0)
    counts = {                                          # chỉ để đủ cột; chưa dùng làm đặc trưng
        "n03": 55 * p1 + 10 * fine, "n05": 16 * p1 + 6 * fine,
        "n10": 3 * p1 + 4 * fine + 0.5 * coarse, "n25": 0.6 * fine + 0.4 * coarse,
        "n50": 0.15 * coarse, "n100": 0.05 * coarse,
    }
    counts = {k: np.round(v * (1 + rng.normal(0, 0.05, n))) for k, v in counts.items()}

    hold = np.repeat(np.arange(0, n, 2), 2)[:n]         # DHT11 cập nhật 2 giây/lần
    h = np.round(np.clip(hum, 5, 95))[hold]
    t = np.round(temp, 1)[hold]
    lx = np.round(np.maximum(lux * (1 + rng.normal(0, 0.01, n)), 0), 1)

    for arr, rate, run in ((p25, 5e-4, 3), (h, 3e-4, 15), (lx, 2e-4, 5)):
        for s0 in np.where(rng.random(n) < rate)[0]:
            arr[s0:s0 + run] = np.nan
    for arr in (p1, p10):
        arr[np.isnan(p25)] = np.nan
    t[np.isnan(h)] = np.nan
    return {"pm1": p1, "pm25": p25, "pm10": p10, **counts, "temp": t, "hum": h, "lux": lx}


def simulate(n_sessions=400, seed=42):
    """Danh sách (dict mảng, meta) của n_sessions phiên, tái lập được theo seed."""
    rng = np.random.default_rng(seed)
    return [simulate_session(rng) for _ in range(n_sessions)]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Sinh dữ liệu mô phỏng nguồn bụi")
    p.add_argument("--sessions", type=int, default=400)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default=str(DATA / "sim_sources.npz"))
    a = p.parse_args(argv)
    sessions = simulate(a.sessions, a.seed)
    arrays = {}
    for i, (arr, _) in enumerate(sessions):
        for k, v in arr.items():
            arrays[f"s{i}_{k}"] = v.astype(np.float32) if k != "label" else v
    np.savez_compressed(a.out, **arrays)
    meta = [m for _, m in sessions]
    Path(a.out).with_name("sim_meta.json").write_text(
        json.dumps({"seed": a.seed, "sessions": meta}, ensure_ascii=False, indent=1), encoding="utf-8")
    secs = np.bincount(np.concatenate([arr["label"] for arr, _ in sessions]), minlength=4)
    for name, s in zip(LABELS, secs):
        print(f"{name:9s} {s / 3600:7.1f} giờ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
