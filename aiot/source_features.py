"""
Đặc trưng cho bộ nhận diện nguồn bụi, tính mỗi 5 giây từ bộ đệm 30 phút mẫu 1 giây.

Dùng chung cho Pi (aiot/source_model.py) và laptop (training/train_source.py), chỉ cần
numpy. Đầu vào là dict {tên cột: mảng float dài WINDOW_S}, phần tử cuối là giây hiện tại,
NaN là thiếu mẫu. Trả None khi 60 giây gần nhất có ít hơn MIN_PM_60 mẫu PM2.5.

Các nhóm đặc trưng:
- mức: PM2.5 5 giây và nền (phân vị 10 của 30 phút), độ vượt nền tương đối;
- động học: độ dốc 30 giây / 5 phút, hệ số biến thiên, số lần nhảy, thời gian vượt nền;
- thành phần hạt: PM1/PM2.5, PM10/PM2.5 của toàn bộ và của phần vượt nền;
- môi trường: độ ẩm, nhiệt độ tăng so với 5 phút trước và so với nền, ánh sáng đổi.

Kênh đếm hạt (n03..n100) được thu nhưng chưa dùng: chưa có số liệu thật để mô phỏng đúng.
"""
import warnings

import numpy as np

WINDOW_S = 1800
STEP_S = 5
MIN_PM_60 = 30
COLUMNS = ("pm1", "pm25", "pm10", "temp", "hum", "lux")

FEATURES = (
    "log_p5", "log_base", "exc_rel5", "exc_rel60", "max300_rel",
    "slope30", "slope300", "cv60", "cv300", "jumps300", "frac_exc300", "since_exc",
    "r1_60", "r10_60", "er1", "er10", "has_exc",
    "dh300", "dh_base", "dt300", "dt_base", "dlux60",
)


def _m(x):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return float(np.nanmean(x)) if x.size else float("nan")


def _q(x, q):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return float(np.nanpercentile(x, q)) if np.isfinite(x).any() else float("nan")


def _sd(x):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return float(np.nanstd(x)) if np.isfinite(x).sum() >= 2 else float("nan")


def _block_means(x, block=STEP_S):
    n = (x.size // block) * block
    if n == 0:
        return np.array([])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(x[x.size - n:].reshape(-1, block), axis=1)


def compute_features(buf: dict) -> np.ndarray | None:
    """Vector đặc trưng theo thứ tự FEATURES, hoặc None khi thiếu dữ liệu PM."""
    p = np.asarray(buf["pm25"], dtype=float)
    if np.isfinite(p[-60:]).sum() < MIN_PM_60:
        return None
    a = np.asarray(buf["pm1"], dtype=float)
    b = np.asarray(buf["pm10"], dtype=float)
    h = np.asarray(buf["hum"], dtype=float)
    t = np.asarray(buf["temp"], dtype=float)
    lx = np.asarray(buf["lux"], dtype=float)

    p5, p60, p300 = _m(p[-5:]), _m(p[-60:]), _m(p[-300:])
    if not np.isfinite(p5):
        p5 = p60
    base = _q(p, 10)
    sc = base + 5.0
    blocks = _block_means(p[-300:])
    thr_jump = 0.15 * sc + 5.0
    d = np.diff(blocks[np.isfinite(blocks)]) if blocks.size else np.array([])
    above = p[-300:] > base * 1.3 + 10.0
    fin300 = np.isfinite(p[-300:])

    # thời gian (giây) kể từ lần cuối PM2.5 TB 5 giây còn gần nền
    all_blocks = _block_means(p)
    near = np.where(np.isfinite(all_blocks) & (all_blocks <= base * 1.15 + 5.0))[0]
    since = (all_blocks.size - 1 - near[-1]) * STEP_S if near.size else WINDOW_S

    a60, b60 = _m(a[-60:]), _m(b[-60:])
    r1, r10 = a60 / (p60 + 1.0), b60 / (p60 + 1.0)
    exc = p60 - base
    has_exc = exc > 0.2 * sc + 5.0
    if has_exc:
        er1 = (a60 - _q(a, 10)) / exc
        er10 = (b60 - _q(b, 10)) / exc
    else:
        er1, er10 = r1, r10

    f = {
        "log_p5": np.log1p(max(p5, 0.0)),
        "log_base": np.log1p(max(base, 0.0)),
        "exc_rel5": (p5 - base) / sc,
        "exc_rel60": (p60 - base) / sc,
        "max300_rel": (np.nanmax(p[-300:]) - base) / sc if fin300.any() else 0.0,
        "slope30": (p5 - _m(p[-35:-30])) / sc,
        "slope300": (p60 - _m(p[-360:-300])) / sc,
        "cv60": _sd(p[-60:]) / (p60 + 5.0),
        "cv300": _sd(p[-300:]) / (p300 + 5.0),
        "jumps300": float(np.sum(np.abs(d) > thr_jump)),
        "frac_exc300": float(above[fin300].mean()) if fin300.any() else 0.0,
        "since_exc": since / WINDOW_S,
        "r1_60": r1, "r10_60": r10, "er1": er1, "er10": er10,
        "has_exc": 1.0 if has_exc else 0.0,
        "dh300": _m(h[-30:]) - _m(h[-330:-300]),
        "dh_base": _m(h[-60:]) - _q(h, 10),
        "dt300": _m(t[-30:]) - _m(t[-330:-300]),
        "dt_base": _m(t[-60:]) - _q(t, 10),
        "dlux60": np.log1p(max(_m(lx[-5:]), 0.0)) - np.log1p(max(_m(lx[-65:-60]), 0.0)),
    }
    vec = np.array([f[k] for k in FEATURES], dtype=float)
    vec[~np.isfinite(vec)] = 0.0
    return np.clip(vec, -50.0, 50.0)


def buffer_from_rows(rows, now: int, window_s: int = WINDOW_S) -> dict:
    """Dựng bộ đệm từ [(ts, {cột: giá trị})] (aiot.db.fast_rows): mảng dài window_s,
    phần tử cuối ứng với giây now, giây không có mẫu là NaN."""
    buf = {c: np.full(window_s, np.nan) for c in COLUMNS}
    start = int(now) - window_s + 1
    for ts, vals in rows:
        i = int(ts) - start
        if 0 <= i < window_s:
            for c in COLUMNS:
                v = vals.get(c)
                if v is not None:
                    buf[c][i] = float(v)
    return buf
