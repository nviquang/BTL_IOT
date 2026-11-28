"""
Sinh dữ liệu 1 giây có nhãn cho mô hình demo (training/train_logreg.py), hiệu chỉnh theo số liệu thật.

Số liệu thật (training/data/, kéo bằng training.export_fast):
- fast_readings.csv: mẫu 1 giây 23/11 21:50 → 28/11 22:43 (phần lớn là ngày 28/11).
- raw_readings.csv: mẫu 10 giây, có đợt đốt nhang 21:07 ngày 23/11.

fit_real() đọc từ số liệu thật:
- nhiễu nền: độ lệch chuẩn và thời gian tương quan của log PM2.5 quanh trung vị trượt 5 phút
  (buổi sáng 28/11 yên tĩnh); tỉ số PM1/PM2.5, PM10/PM2.5 của nền;
- dải độ ẩm, nhiệt độ; dải mức nền mở rộng ra 5–250 µg/m³ (thật: ~27 đến ~170);
- hai ĐƯỜNG CONG BỤI THẬT dùng làm mẫu sự kiện "bụi mịn": nhang 23/11 (10 s, nội suy lên 1 s)
  và gai bụi 08:20 ngày 28/11 (1 s): tỉ số vượt nền của PM2.5 và tỉ phần PM1, PM10 của phần vượt.

session() sinh một phiên: BASE_S giây đầu chỉ có nền (để bộ đệm nền đầy), sau đó là sự kiện
theo `kind`. Nấu ăn CHƯA CÓ SỐ ĐO THẬT: mô phỏng theo hơi nước (ẩm +5–20 %, nhiệt +0,5–3 °C,
PMS7003 đếm cả giọt hơi nên PM tăng ×1,5–4 cùng nhịp độ ẩm).

Nhãn từng giây ("thầy" tất định) tính trên tín hiệu SẠCH (chưa có nhiễu cảm biến) với đúng
định nghĩa nền của aiot/source_window.py (phân vị 10 của BASE_S giây):
  bụi = (PM2.5 + 5) / (nền + 5) ≥ DUST_RATIO (cùng PM_OFFSET với đặc trưng); nóng ẩm = ẩm − nền ≥ HUM_RISE hoặc nhiệt − nền ≥ TEMP_RISE
  cooking   khi (PM2.5 + 5) / (nền + 5) ≥ COOK_RATIO và nóng ẩm
  pollution khi bụi và không nóng ẩm
  normal    còn lại (kể cả ẩm tăng một mình, nhiệt tăng một mình, bụi tăng nhẹ)

Trên Pi (không có pandas, không có CSV): load_real() đọc tham số đã rút sẵn ở REAL_JSON
(training/train_logreg.py ghi mỗi lần huấn luyện); session() chỉ cần numpy.
"""
import json
from pathlib import Path

import numpy as np

from aiot.source_model import LABELS
from aiot.source_window import BASE_S, COLUMNS, PM_OFFSET

DATA = Path(__file__).resolve().parent / "data"
REAL_JSON = DATA / "demo_real.json"

DUST_RATIO = 1.3
COOK_RATIO = 1.2
HUM_RISE = 3.0
TEMP_RISE = 1.5
LEVEL_RANGE = (5.0, 250.0)
PM_MAX = 2000.0                 # đỉnh của tín hiệu sạch (thật: 1061 và 1513)
PM_SENSOR_MAX = 1000.0          # aiot.db.PM_RANGE[1]: fast_rows ghim số đọc bão hoà về mức này
LUX_LEVELS = (3.0, 30.0, 180.0, 600.0, 1030.0)

# Hai sự kiện bụi thật (ts epoch, giờ VN): nhang 23/11 21:07 (raw 10 s), gai bụi 28/11 08:20 (1 s)
REAL_DUST = (("raw_readings.csv", 1795442800, 1795442990, 10),
             ("fast_readings.csv", 1795828800, 1795828960, 1))
QUIET = ("fast_readings.csv", 1795829400, 1795842400)   # 28/11 08:30–12:06, không có sự kiện

KINDS = ("idle", "cooking", "dust", "hum_only", "temp_only", "small_bump", "step_up",
         "step_down", "back_to_back")
NORMAL_ONLY = ("idle", "hum_only", "temp_only", "small_bump", "step_down")


# ---------------- số liệu thật ----------------
def _series(file, t0, t1, hold, data_dir):
    import pandas as pd
    df = pd.read_csv(data_dir / file).sort_values("ts").drop_duplicates("ts")
    df = df[(df.ts >= t0) & (df.ts <= t1)].set_index("ts")
    return df.reindex(range(int(t0), int(t1) + 1)).ffill(limit=hold - 1 if hold > 1 else None)


def _template(s):
    """Mẫu sự kiện bụi từ chuỗi 1 giây: ex = PM2.5/nền − 1, f1/f10 = tỉ phần PM1/PM10 của phần
    vượt nền (so với PM2.5 vượt nền), tính trong đoạn có phần vượt."""
    base = {c: float(np.nanmedian(s[c].to_numpy()[:30])) for c in ("pm1", "pm25", "pm10")}
    ex = np.clip(s["pm25"].to_numpy(float) / base["pm25"] - 1.0, 0.0, None)
    ex = np.nan_to_num(ex)
    big = ex > 0.5
    exc = {c: s[c].to_numpy(float) - base[c] for c in ("pm1", "pm25", "pm10")}
    f1 = float(np.nanmedian(exc["pm1"][big] / exc["pm25"][big]))
    f10 = float(np.nanmedian(exc["pm10"][big] / exc["pm25"][big]))
    nz = np.where(ex > 0.05)[0]
    ex = ex[max(nz[0] - 2, 0):nz[-1] + 3]
    return {"ex": ex, "f1": f1, "f10": f10, "peak": float(ex.max())}


def fit_real(data_dir: Path = DATA) -> dict:
    import pandas as pd
    q = _series(QUIET[0], QUIET[1], QUIET[2], 1, data_dir)
    lp = np.log1p(q["pm25"])
    res = (lp - lp.rolling(301, center=True, min_periods=100).median()).dropna()
    rho10 = float(res.autocorr(10))
    tau = -10.0 / np.log(min(max(rho10, 0.05), 0.95))
    fast = pd.read_csv(data_dir / "fast_readings.csv")
    templates = [_template(_series(f, a, b, hold, data_dir)) for f, a, b, hold in REAL_DUST]
    return {
        "noise_sd": float(res.std()), "noise_tau": float(tau),
        "r1": float((q.pm1 / q.pm25).median()), "r10": float((q.pm10 / q.pm25).median()),
        "hum_range": (float(fast.hum.quantile(0.02)), float(fast.hum.quantile(0.98))),
        "temp_range": (float(fast.temp.quantile(0.02)), float(fast.temp.quantile(0.98))),
        "level_range": LEVEL_RANGE, "dust": templates,
    }


def save_real(real: dict, path: Path = REAL_JSON) -> None:
    out = dict(real)
    out["dust"] = [{**t, "ex": np.round(t["ex"], 5).tolist()} for t in real["dust"]]
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def load_real(data_dir: Path = DATA) -> dict:
    """fit_real() khi có CSV và pandas (laptop), ngược lại đọc REAL_JSON (Pi)."""
    try:
        return fit_real(data_dir)
    except (ImportError, FileNotFoundError):
        real = json.loads((data_dir / REAL_JSON.name).read_text(encoding="utf-8"))
        real["dust"] = [{**t, "ex": np.asarray(t["ex"], float)} for t in real["dust"]]
        return real


# ---------------- mô phỏng ----------------
def _ou(rng, n, sd, tau):
    a = np.exp(-1.0 / tau)
    e = rng.normal(0.0, sd * np.sqrt(1 - a * a), n)
    x = np.empty(n)
    x[0] = rng.normal(0.0, sd)
    for i in range(1, n):
        x[i] = a * x[i - 1] + e[i]
    return x


def _first_order(target, tau):
    """Đáp ứng bậc một của tín hiệu đích (tau giây)."""
    a = 1.0 - np.exp(-1.0 / max(tau, 1e-3))
    y = np.empty_like(target)
    acc = 0.0
    for i, v in enumerate(target):
        acc += a * (v - acc)
        y[i] = acc
    return y


def _rolling_p10(x, w=BASE_S):
    """Phân vị 10 của w giây gần nhất (tính cả giây hiện tại), như np.nanpercentile trong
    aiot/source_window.py. w − 1 giây đầu (chưa đủ cửa sổ) dùng phân vị của phần đã có."""
    x = np.asarray(x, float)
    try:
        import pandas as pd             # laptop: nhanh hơn ~30 lần, cùng kết quả
        return pd.Series(x).rolling(w, min_periods=1).quantile(0.1).to_numpy()
    except ImportError:
        pass
    out = np.empty_like(x)
    if len(x) >= w:
        out[w - 1:] = np.percentile(np.lib.stride_tricks.sliding_window_view(x, w), 10, axis=1)
    for i in range(min(w - 1, len(x))):
        out[i] = np.percentile(x[:i + 1], 10)
    return out


def _dust_event(rng, real, n, start):
    """Phần vượt nền PM2.5 (tỉ số) và tỉ phần PM1, PM10 từ một đường cong bụi thật, co giãn."""
    ex = np.zeros(n)
    tpl = real["dust"][rng.integers(len(real["dust"]))]
    stretch = rng.uniform(0.5, 4.0)
    # biên độ trên thang log; đỉnh tối đa PM_MAX (thật: 1061 và 1513)
    amp = min(rng.uniform(0.3, 1.2), np.log(PM_MAX / 250.0) / np.log1p(tpl["peak"]) * 1.5)
    t = start
    for _ in range(rng.integers(1, 4)):
        m = int(len(tpl["ex"]) * stretch)
        curve = np.interp(np.linspace(0, len(tpl["ex"]) - 1, m), np.arange(len(tpl["ex"])), tpl["ex"])
        curve = np.expm1(np.log1p(curve) * amp)          # biên độ trên thang log
        seg = curve[:max(0, min(m, n - t))]
        ex[t:t + len(seg)] = np.maximum(ex[t:t + len(seg)], seg)
        t += m + int(rng.uniform(20, 120))
        if t >= n:
            break
    f1 = tpl["f1"] * rng.uniform(0.8, 1.2)
    f10 = tpl["f10"] * rng.uniform(0.85, 1.15)
    return ex, f1, f10, t


def _cooking_event(rng, n, start, with_pm=True):
    """(ex PM2.5, Δẩm, Δnhiệt, lúc kết thúc) của một lần nấu / đun nước."""
    hold = int(rng.uniform(60, 600))
    on = np.zeros(n)
    on[start:min(n, start + hold)] = 1.0
    tau_up, tau_down = rng.uniform(15, 60), rng.uniform(60, 180)
    # đáp ứng lên nhanh, xuống chậm: hai bậc một nối nhau
    shape = np.where(np.arange(n) < start + hold, _first_order(on, tau_up), 0.0)
    after = np.arange(n) >= start + hold
    if after.any():
        k = np.argmax(after)
        last = shape[k - 1] if k > 0 else 0.0
        shape[after] = last * np.exp(-(np.arange(after.sum())) / tau_down)
    a_h = rng.uniform(5.0, 20.0)
    dh = a_h * shape
    dt = rng.uniform(0.5, 3.0) * _first_order(shape, rng.uniform(15, 45))
    # giọt hơi nước: chỉ có bụi khi độ ẩm đã vượt nền HUM_RISE + 1 %; PM lên chậm hơn độ ẩm
    # (hơi phải lan tới cảm biến) và xuống rất nhanh (giọt bay hơi ngay khi tắt bếp), nên không
    # có lúc "bụi mà chưa ẩm" ở đầu và cuối lần nấu
    gate = np.clip((dh - HUM_RISE - 1.0) / (a_h - HUM_RISE - 1.0), 0.0, 1.0)
    ex = (rng.uniform(0.5, 3.0) * _asym_first_order(gate ** 1.5, rng.uniform(20, 60),
                                                    rng.uniform(2, 5))
          if with_pm else np.zeros(n))
    return ex, dh, dt, start + hold + int(4 * tau_down)


def _asym_first_order(target, tau_up, tau_down):
    a_up, a_dn = 1.0 - np.exp(-1.0 / tau_up), 1.0 - np.exp(-1.0 / tau_down)
    y = np.empty_like(target)
    acc = 0.0
    for i, v in enumerate(target):
        acc += (a_up if v > acc else a_dn) * (v - acc)
        y[i] = acc
    return y


def session(rng, real, kind, duration_s=None):
    """Một phiên 1 giây: dict cột COLUMNS (đã qua mô hình cảm biến) + 'label' (mã theo LABELS)."""
    if kind not in KINDS:
        raise ValueError(f"kind phải thuộc {KINDS}")
    n = duration_s or BASE_S + (2400 if kind == "step_up" else int(rng.uniform(900, 1500)))
    start = BASE_S + int(rng.uniform(60, 240))
    idx = np.arange(n)

    # nền sạch: mức log-uniform, trôi chậm ±10 %/giờ; ẩm/nhiệt trong dải thật, trôi chậm
    lo, hi = real["level_range"]
    level = np.exp(rng.uniform(np.log(lo), np.log(hi)))
    bg = level * np.exp(rng.uniform(-0.1, 0.1) * idx / 3600 + _ou(rng, n, 0.03, 900))
    h0 = rng.uniform(*real["hum_range"])
    t0 = rng.uniform(*real["temp_range"])
    hum = h0 + rng.uniform(-3, 3) * idx / 3600 + _ou(rng, n, 0.4, 600)
    temp = t0 + rng.uniform(-1.0, 1.0) * idx / 3600 + _ou(rng, n, 0.08, 600)

    ex = np.zeros(n)
    f1, f10 = real["r1"], real["r10"]
    if kind in ("dust", "back_to_back"):
        ex, f1, f10, end = _dust_event(rng, real, n, start)
        if kind == "back_to_back":
            c_ex, dh, dt, _ = _cooking_event(rng, n, min(end + int(rng.uniform(60, 120)), n - 300))
            ex, hum, temp = ex + c_ex, hum + dh, temp + dt
    elif kind in ("cooking", "hum_only"):
        c_ex, dh, dt, _ = _cooking_event(rng, n, start, with_pm=kind == "cooking")
        if kind == "hum_only":
            dt *= rng.uniform(0.0, 0.5)
        ex, hum, temp = c_ex, hum + dh, temp + dt
        f1, f10 = rng.uniform(0.6, 0.8), rng.uniform(1.1, 1.4)
    elif kind == "temp_only":
        on = (idx >= start) & (idx < start + int(rng.uniform(120, 900)))
        temp = temp + rng.uniform(1.0, 3.0) * _first_order(on.astype(float), rng.uniform(30, 120))
    elif kind == "small_bump":
        on = (idx >= start) & (idx < start + int(rng.uniform(10, 60)))
        ex = rng.uniform(0.05, 0.15) * _first_order(on.astype(float), 5.0)
    elif kind in ("step_up", "step_down"):
        k = rng.uniform(1.6, 3.5) if kind == "step_up" else 1.0 / rng.uniform(1.6, 3.5)
        step = _first_order((idx >= start).astype(float), rng.uniform(20, 120))
        bg = bg * (1.0 + (k - 1.0) * step)
        hum = hum - rng.uniform(0, 7) * step          # 16:05 ngày 28/11: ẩm giảm khi nền nhảy

    pm25 = np.minimum(bg * (1.0 + ex), PM_MAX)
    ex = pm25 / bg - 1.0
    pm1 = bg * real["r1"] + bg * ex * f1
    pm10 = bg * real["r10"] + bg * ex * f10

    # nhãn từ tín hiệu sạch, cùng định nghĩa nền với aiot/source_window.py
    ratio = (pm25 + PM_OFFSET) / (_rolling_p10(pm25) + PM_OFFSET)
    warm = (hum - _rolling_p10(hum) >= HUM_RISE) | (temp - _rolling_p10(temp) >= TEMP_RISE)
    label = np.full(n, LABELS.index("normal"))
    label[(ratio >= DUST_RATIO) & ~warm] = LABELS.index("pollution")
    label[(ratio >= COOK_RATIO) & warm] = LABELS.index("cooking")
    label[:BASE_S] = LABELS.index("normal")

    out = _sensor(rng, real, pm1, pm25, pm10, temp, hum, n)
    out["label"] = label
    return out


def _sensor(rng, real, pm1, pm25, pm10, temp, hum, n):
    """PMS7003: nhiễu nhân tính tương quan (đo từ số liệu thật), số nguyên, khung lỗi đơn lẻ,
    mất mẫu. DHT11: ẩm làm tròn 1 %, nhiệt 0,1 °C, cập nhật 2 giây/lần."""
    noise = _ou(rng, n, real["noise_sd"], real["noise_tau"])
    out = {}
    for c, x in (("pm1", pm1), ("pm25", pm25), ("pm10", pm10)):
        own = _ou(rng, n, real["noise_sd"] * 0.4, real["noise_tau"])
        out[c] = np.minimum(np.round(x * np.exp(noise + own)), PM_SENSOR_MAX)
    bad = rng.random(n) < 0.001
    for c in ("pm1", "pm25", "pm10"):
        out[c][bad] *= rng.uniform(2, 4, bad.sum())
    hold2 = np.repeat(np.arange(0, n, 2), 2)[:n]
    out["hum"] = np.round(hum + rng.normal(0, 0.2, n))[hold2]
    out["temp"] = np.round(temp + rng.normal(0, 0.03, n), 1)[hold2]
    lux = np.full(n, rng.choice(LUX_LEVELS)) * np.exp(rng.normal(0, 0.01, n))
    if rng.random() < 0.3:
        k = int(rng.uniform(BASE_S - 600, n))
        lux[k:] = rng.choice(LUX_LEVELS)
    out["lux"] = lux
    miss = rng.random(n) < 0.002
    if rng.random() < 0.2:
        k = int(rng.uniform(BASE_S, n - 60))
        miss[k:k + int(rng.uniform(5, 40))] = True
    for c in COLUMNS:
        out[c] = out[c].astype(float)
        out[c][miss] = np.nan
    return out
