"""
Đặc trưng cửa sổ cho bộ nhận diện nguồn bụi (logistic regression, aiot/source_model.py).

Ý tưởng: "bất thường" là LỆCH SO VỚI NỀN PHÒNG, không phải mức tuyệt đối. Nền là phân vị 10 của
BASE_S = 30 phút gần nhất cho từng kênh PM2.5, độ ẩm, nhiệt độ. Mô hình nhìn SEQ_S = 120 giây gần
nhất, chia N_BLOCKS = 24 khối 5 giây; mỗi khối cho:
- d25_k: log(PM2.5 TB khối + 5) − log(nền PM2.5 + 5)  (tỉ lệ so với nền, không phụ thuộc mức
         nền; +5 µg/m³ để nền rất sạch không nhảy vì PMS7003 chỉ trả số nguyên)
- ch_k:  max(d25_k − 0,1; 0) × max(ẩm TB khối − nền ẩm; 0)      ("bụi kèm ẩm tăng")
- ct_k:  max(d25_k − 0,1; 0) × max(nhiệt TB khối − nền nhiệt; 0) ("bụi kèm nhiệt tăng")
Dùng tích thay cho chuỗi ẩm/nhiệt thô để mô hình tuyến tính tách được: ẩm tăng một mình → 0
(bình thường), bụi một mình → chỉ d25 (bụi mịn), bụi + ẩm/nhiệt → ch/ct lớn (nấu ăn).
Cộng thêm: lớn nhất của 30 s gần nhất cho d25, ch, ct; cú nhảy lớn nhất tính theo z-score so
với độ dao động thường ngày của phòng; tỉ lệ khối thiếu mẫu.

Không có đặc trưng mức PM tuyệt đối: nền 20 hay 200 µg/m³, để yên đều là "bình thường".

Chỉ cần numpy (chạy trên Pi). Đầu vào: dict {cột: mảng float dài BASE_S}, phần tử cuối là
giây hiện tại, NaN là thiếu mẫu (buffer_from_rows dựng từ aiot.db.fast_rows).
"""
import warnings

import numpy as np

BASE_S = 1800
SEQ_S = 120
STEP_S = 5
N_BLOCKS = SEQ_S // STEP_S
MIN_PM_60 = 30
COLUMNS = ("pm1", "pm25", "pm10", "temp", "hum", "lux")
RECENT_BLOCKS = 6                       # 30 giây gần nhất cho các số tóm tắt
PM_OFFSET = 5.0                         # µg/m³ cộng vào trước khi lấy log

WINDOW_FEATURES = (
    tuple(f"d25_{k:02d}" for k in range(N_BLOCKS))
    + tuple(f"ch_{k:02d}" for k in range(N_BLOCKS))
    + tuple(f"ct_{k:02d}" for k in range(N_BLOCKS))
    + ("d25_max", "ch_max", "ct_max", "max_jump_z", "miss_frac")
)


def _q(x, q):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return float(np.nanpercentile(x, q)) if np.isfinite(x).any() else float("nan")


def _blocks(x, n_blocks, block=STEP_S):
    """TB từng khối block giây của n_blocks khối cuối (NaN khi khối không có mẫu)."""
    n = n_blocks * block
    seg = np.asarray(x[-n:], dtype=float)
    if seg.size < n:
        seg = np.concatenate([np.full(n - seg.size, np.nan), seg])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(seg.reshape(-1, block), axis=1)


def _ffill(x):
    """NaN lấy giá trị hữu hạn liền trước; NaN ở đầu mảng thành 0."""
    x = np.asarray(x, dtype=float).copy()
    last = 0.0
    for i, v in enumerate(x):
        if np.isfinite(v):
            last = v
        else:
            x[i] = last
    return x


def compute_window_features(buf: dict) -> np.ndarray | None:
    """Vector đặc trưng theo thứ tự WINDOW_FEATURES, hoặc None khi 60 s gần nhất có ít hơn
    MIN_PM_60 mẫu PM2.5."""
    p = np.asarray(buf["pm25"], dtype=float)
    if np.isfinite(p[-60:]).sum() < MIN_PM_60:
        return None
    h = np.asarray(buf["hum"], dtype=float)
    t = np.asarray(buf["temp"], dtype=float)

    b25 = max(_q(p, 10), 0.0)
    bh, bt = _q(h, 10), _q(t, 10)

    # khối không có mẫu lấy giá trị của khối liền trước (không coi là "về nền": một lỗ hổng
    # không được kéo mô hình về bình thường giữa lúc đang có khói); khối thiếu ở đầu cửa sổ = 0
    lp = np.log(np.maximum(_blocks(p, N_BLOCKS), 0.0) + PM_OFFSET)
    miss = ~np.isfinite(lp)
    d25 = _ffill(lp - np.log(b25 + PM_OFFSET))
    dh = _ffill(_blocks(h, N_BLOCKS) - bh) if np.isfinite(bh) else np.zeros(N_BLOCKS)
    dt = _ffill(_blocks(t, N_BLOCKS) - bt) if np.isfinite(bt) else np.zeros(N_BLOCKS)

    # tích từng khối "bụi vượt nền × ẩm/nhiệt tăng": chỉ tính phần bụi vượt nền trên 10 %
    # (log 0,1). Ẩm hoặc nhiệt tăng MỘT MÌNH cho tích bằng 0, nên mô hình tuyến tính không thể
    # học "ẩm tăng là nấu ăn" mà phải có bụi đi kèm.
    dust_k = np.maximum(d25 - 0.1, 0.0)
    ch = dust_k * np.maximum(dh, 0.0)
    ct = dust_k * np.maximum(dt, 0.0)

    r = slice(N_BLOCKS - RECENT_BLOCKS, N_BLOCKS)
    d25_max = d25[r].max()

    # cú nhảy theo z-score: |Δ| giữa hai khối liên tiếp trong cửa sổ so với độ dao động của
    # cả 30 phút (MAD của Δ khối, có sàn 0,02 để phòng rất ổn định không cho z quá lớn)
    all_lp = np.log(np.maximum(_blocks(p, p.size // STEP_S), 0.0) + PM_OFFSET)
    all_d = np.diff(all_lp[np.isfinite(all_lp)])
    mad = float(np.median(np.abs(all_d - np.median(all_d)))) if all_d.size else 0.0
    win_d = np.diff(lp[~miss]) if (~miss).sum() >= 2 else np.array([0.0])
    jump_z = float(np.max(np.abs(win_d))) / (1.4826 * mad + 0.02)

    vec = np.concatenate([
        d25, ch, ct,
        [d25_max, ch[r].max(), ct[r].max(), jump_z, float(miss.mean())],
    ])
    vec[~np.isfinite(vec)] = 0.0
    return np.clip(vec, -50.0, 50.0)


def buffer_from_rows(rows, now: int, window_s: int = BASE_S) -> dict:
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
