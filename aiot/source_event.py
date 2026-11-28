"""
Nhận diện nguồn bụi theo SỰ KIỆN cho profile demo (docs/de_xuat_ai_demo_v3.md).

Khác aiot/source_features.py (phân loại cửa sổ 30 phút mỗi 5 giây), ở đây:
- Nền phòng là trung bình trượt mũ (hằng số thời gian base_tau_s) của từng kênh, chỉ cập
  nhật khi IDLE, đóng băng khi có sự kiện. calibrate() chốt nền về mẫu hiện tại.
- Máy trạng thái IDLE -> EVENT -> COOLDOWN -> IDLE, chạy trên từng mẫu 1 giây.
- Trong EVENT có hai cách mô tả sự kiện:
  * features(): 8 số tóm tắt cộng dồn từ lúc bắt đầu (EVENT_FEATURES), dùng để giải thích;
  * shape(): ĐƯỜNG CONG nhiều kênh của cả cửa sổ, từ pre_s giây trước lúc vượt ngưỡng tới
    giây hiện tại (SHAPE_CHANNELS, mỗi giây một hàng, so với nền). Mô hình kNN-DTW
    (aiot.source_model.ShapeKnnModel) so hình dạng theo thời gian thay vì vài ngưỡng.
  Từ giây classify_from_s gọi hàm phân loại mỗi giây, tới giây lock_after_s thì khoá nhãn
  tới hết sự kiện. IDLE: nhãn 'normal'.

Hàm thuần, chỉ cần numpy. Trạng thái TrackerState lưu được thành JSON (khoá state
'event_state' của controller) và dùng y hệt khi phát lại dữ liệu trên laptop
(training/train_demo.py).

classify(trạng thái) -> (nhãn | None, độ tin cậy); mô hình có sẵn classify_event. Nhãn None = mô hình không chắc
("chưa gặp kiểu này"), controller dùng luật ngưỡng.
"""
import dataclasses
import json
import math
from dataclasses import dataclass, field

import numpy as np

EVENT_FEATURES = ("rise_rate", "peak_rel", "ex_r1", "ex_r10", "coarse_frac",
                  "d_hum", "d_temp", "spikiness")
BASE_COLUMNS = ("pm1", "pm25", "pm10", "n03", "n50", "n100", "temp", "hum")
PHASES = ("idle", "event", "cooldown")
RING_N = 6                              # giữ 6 mẫu PM2.5 gần nhất để tính độ tăng 5 giây
SEQ_COLUMNS = ("pm1", "pm25", "pm10", "coarse", "hum", "temp")   # coarse = n50 + n100
SHAPE_CHANNELS = ("pm1_rel", "pm25_rel", "pm10_rel", "coarse_rel", "d_hum", "d_temp")


@dataclass(frozen=True)
class EventCfg:
    base_tau_s: float = 300.0           # hằng số thời gian của nền (giây)
    enter_abs: float = 15.0             # vào EVENT: PM2.5 > nền + max(enter_abs, enter_rel·nền)
    enter_rel: float = 0.20
    enter_hum: float = 3.0              # ...hoặc độ ẩm > nền + 3 %
    enter_n: int = 3                    # ...liên tục 3 mẫu
    exit_abs: float = 5.0               # về nền: PM2.5 < nền + max(exit_abs, exit_rel·nền)
    exit_rel: float = 0.10
    exit_hum: float = 2.0               # ...và độ ẩm < nền + 2 %
    exit_hold_s: int = 20               # ...liên tục 20 giây thì sang COOLDOWN
    cooldown_s: int = 20                # COOLDOWN 20 giây rồi về IDLE
    max_event_s: int = 600              # sự kiện quá 10 phút: kết thúc, lấy lại nền
    classify_from_s: int = 8            # bắt đầu đoán ở giây thứ 8 của sự kiện
    lock_after_s: int = 30              # khoá nhãn từ giây thứ 30
    max_gap_s: int = 30                 # mất mẫu lâu hơn: huỷ sự kiện, giữ nền
    pre_s: int = 10                     # đường cong bắt đầu 10 giây trước lúc vượt ngưỡng
    min_peak_ratio: float = 5.0         # phần vượt PM2.5 nhỏ hơn: không tính các tỉ lệ hạt
    jump_abs: float = 10.0              # một "gai": PM2.5 tăng > max(jump_abs, jump_rel·nền)
    jump_rel: float = 0.15              # trong 5 giây (sau khi đã giảm lại)


def _f(v):
    """float hoặc NaN (None/NaN/không phải số)."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return math.nan
    return x if math.isfinite(x) else math.nan


@dataclass(frozen=True)
class TrackerState:
    phase: str = "idle"
    last_ts: int | None = None
    base: dict = field(default_factory=dict)        # {kênh: nền}
    ring: tuple = ()                                # PM2.5 các giây gần nhất
    trigger_n: int = 0                              # số mẫu liên tiếp vượt ngưỡng vào
    start_ts: int | None = None                     # lúc bắt đầu sự kiện
    calm_since: int | None = None                   # EVENT: lúc bắt đầu về gần nền
    cooldown_since: int | None = None
    acc: dict = field(default_factory=dict)         # bộ cộng dồn của sự kiện
    label: str | None = "normal"
    confidence: float | None = None
    locked: bool = False
    n_events: int = 0                               # số sự kiện đã bắt đầu (để đánh số)
    pre: tuple = ()                                 # pre_s mẫu gần nhất (SEQ_COLUMNS)
    seq: tuple = ()                                 # đường cong sự kiện, tối đa tới giây khoá

    def to_json(self) -> str:
        d = dataclasses.asdict(self)
        d["ring"] = list(self.ring)
        d["pre"] = [list(r) for r in self.pre]
        d["seq"] = [list(r) for r in self.seq]
        return json.dumps(d, sort_keys=True)

    @classmethod
    def from_json(cls, text):
        """Đọc lại; thiếu/hỏng thì trạng thái rỗng (nền học lại từ đầu)."""
        try:
            d = json.loads(text)
            if d.get("phase") not in PHASES:
                return cls()
            d["ring"] = tuple(float(v) for v in d.get("ring", ()))
            for k in ("pre", "seq"):
                d[k] = tuple(tuple(None if v is None else float(v) for v in r)
                             for r in d.get(k, ()) if len(r) == len(SEQ_COLUMNS))
            names = {f.name for f in dataclasses.fields(cls)}
            return cls(**{k: v for k, v in d.items() if k in names})
        except (TypeError, ValueError, KeyError, AttributeError):
            return cls()

    @property
    def age_s(self) -> int | None:
        if self.start_ts is None or self.last_ts is None:
            return None
        return self.last_ts - self.start_ts


def calibrate(st: TrackerState, vals: dict, ts: int | None = None) -> TrackerState:
    """Chốt nền về mẫu hiện tại (lệnh /calibrate), huỷ sự kiện đang có."""
    base = {c: _f(vals.get(c)) for c in BASE_COLUMNS}
    base = {c: v for c, v in base.items() if not math.isnan(v)}
    return TrackerState(base=base, last_ts=ts if ts is not None else st.last_ts,
                        n_events=st.n_events)


def _new_acc():
    return {"n": 0, "s_pm1": 0.0, "s_pm25": 0.0, "s_pm10": 0.0, "n_pm1": 0, "n_pm10": 0,
            "s_n03": 0.0, "s_coarse": 0.0, "n_cnt": 0,
            "peak": 0.0, "dhum": 0.0, "dtemp": 0.0, "rise": 0.0, "jumps": 0, "armed": True}


def features(st: TrackerState, cfg: EventCfg = EventCfg()) -> np.ndarray | None:
    """Vector EVENT_FEATURES của sự kiện đang có, None khi không ở EVENT/COOLDOWN."""
    a = st.acc
    if st.phase == "idle" or not a or a.get("n", 0) == 0:
        return None
    sc = st.base.get("pm25", 0.0) + 5.0
    peak = a["peak"]
    if peak >= cfg.min_peak_ratio and a["s_pm25"] > 0:
        r1 = a["s_pm1"] / a["s_pm25"] if a["n_pm1"] else 0.0
        r10 = a["s_pm10"] / a["s_pm25"] if a["n_pm10"] else 0.0
        coarse = a["s_coarse"] / a["s_n03"] if a["n_cnt"] and a["s_n03"] > 0 else 0.0
    else:
        r1 = r10 = coarse = 0.0
    dur_min = max((st.age_s or 0) / 60.0, 0.5)
    vec = np.array([a["rise"] / sc, peak / sc, r1, r10, coarse, a["dhum"], a["dtemp"],
                    a["jumps"] / dur_min], dtype=float)
    vec[~np.isfinite(vec)] = 0.0
    return np.clip(vec, [0, 0, -1, -1, -1, -5, -5, 0], [50, 50, 3, 10, 5, 50, 20, 60])


def _slog(v):
    return np.sign(v) * np.log1p(np.abs(v))


def shape(st: TrackerState) -> np.ndarray | None:
    """Đường cong sự kiện (số giây × SHAPE_CHANNELS) so với nền, None khi IDLE.

    PM và đếm hạt thô: log có dấu của (giá trị − nền)/(nền + hằng số), nên đỉnh 1000 không
    lấn hết đỉnh 50; độ ẩm (%) chia 5, nhiệt độ (°C) giữ nguyên. Thiếu mẫu: lấy giá trị trước."""
    if st.phase == "idle" or not st.seq:
        return None
    b = st.base
    bc = b.get("n50", math.nan) + b.get("n100", math.nan)
    refs = ((b.get("pm1", math.nan), 5.0), (b.get("pm25", math.nan), 5.0),
            (b.get("pm10", math.nan), 5.0), (bc, 10.0))
    out = np.zeros((len(st.seq), len(SHAPE_CHANNELS)))
    prev = np.zeros(len(SHAPE_CHANNELS))
    for i, r in enumerate(st.seq):
        row = prev.copy()
        for j, (ref, add) in enumerate(refs):
            v = r[j]
            if v is not None and math.isfinite(ref):
                row[j] = _slog((v - ref) / (ref + add))
        if r[4] is not None and "hum" in b:
            row[4] = (r[4] - b["hum"]) / 5.0
        if r[5] is not None and "temp" in b:
            row[5] = r[5] - b["temp"]
        out[i] = prev = row
    return out


def _seq_row(vals: dict) -> tuple:
    def g(v):
        return None if math.isnan(v) else v
    coarse = vals["n50"] + vals["n100"]
    return (g(vals["pm1"]), g(vals["pm25"]), g(vals["pm10"]), g(coarse), g(vals["hum"]),
            g(vals["temp"]))


def _update_base(base: dict, vals: dict, alpha: float) -> dict:
    out = dict(base)
    for c in BASE_COLUMNS:
        v = vals.get(c)
        if math.isnan(v):
            continue
        out[c] = v if c not in out else out[c] + alpha * (v - out[c])
    return out


def _accumulate(acc: dict, vals: dict, base: dict, ring: tuple, cfg: EventCfg) -> dict:
    a = dict(acc)
    a["n"] += 1
    b25 = base.get("pm25", 0.0)
    ex25 = vals["pm25"] - b25
    a["s_pm25"] += ex25
    a["peak"] = max(a["peak"], ex25)
    if not math.isnan(vals["pm1"]) and "pm1" in base:
        a["s_pm1"] += vals["pm1"] - base["pm1"]
        a["n_pm1"] += 1
    if not math.isnan(vals["pm10"]) and "pm10" in base:
        a["s_pm10"] += vals["pm10"] - base["pm10"]
        a["n_pm10"] += 1
    cnt = [vals[c] for c in ("n03", "n50", "n100")]
    if not any(math.isnan(v) for v in cnt) and all(c in base for c in ("n03", "n50", "n100")):
        a["s_n03"] += cnt[0] - base["n03"]
        a["s_coarse"] += cnt[1] + cnt[2] - base["n50"] - base["n100"]
        a["n_cnt"] += 1
    if not math.isnan(vals["hum"]) and "hum" in base:
        a["dhum"] = max(a["dhum"], vals["hum"] - base["hum"])
    if not math.isnan(vals["temp"]) and "temp" in base:
        a["dtemp"] = max(a["dtemp"], vals["temp"] - base["temp"])
    if len(ring) >= RING_N:
        d5 = ring[-1] - ring[0]
        a["rise"] = max(a["rise"], d5)
        if a["armed"] and d5 > max(cfg.jump_abs, cfg.jump_rel * b25):
            a["jumps"] += 1
            a["armed"] = False
        elif d5 < 0:
            a["armed"] = True
    return a


def _idle_after(st: TrackerState, ts: int, base: dict | None = None) -> TrackerState:
    return TrackerState(phase="idle", last_ts=ts, base=st.base if base is None else base,
                        ring=st.ring, pre=st.pre, n_events=st.n_events)


def update(st: TrackerState, ts: int, sample: dict, classify=None,
           cfg: EventCfg = EventCfg()) -> TrackerState:
    """Một mẫu 1 giây. sample: {kênh: giá trị | None}. classify(state) -> (nhãn|None, p).

    Mẫu không có PM2.5 bị bỏ qua (chỉ cập nhật last_ts khi đã có). Mẫu cũ hơn last_ts bị bỏ."""
    ts = int(ts)
    if st.last_ts is not None and ts <= st.last_ts:
        return st
    vals = {c: _f(sample.get(c)) for c in BASE_COLUMNS}
    if math.isnan(vals["pm25"]):
        return st
    if st.last_ts is not None and ts - st.last_ts > cfg.max_gap_s and st.phase != "idle":
        st = _idle_after(st, st.last_ts)
    if st.last_ts is not None and ts - st.last_ts > cfg.max_gap_s:
        st = dataclasses.replace(st, ring=(), pre=())
    ring = (st.ring + (vals["pm25"],))[-RING_N:]
    row = _seq_row(vals)
    pre = (st.pre + (row,))[-max(1, cfg.pre_s):]

    if "pm25" not in st.base:                       # mẫu đầu tiên: lấy làm nền
        return TrackerState(base=_update_base({}, vals, 1.0), last_ts=ts, ring=ring, pre=pre,
                            n_events=st.n_events)

    b25 = st.base["pm25"]
    over_pm = vals["pm25"] > b25 + max(cfg.enter_abs, cfg.enter_rel * b25)
    over_hum = (not math.isnan(vals["hum"]) and "hum" in st.base
                and vals["hum"] > st.base["hum"] + cfg.enter_hum)
    calm = (vals["pm25"] < b25 + max(cfg.exit_abs, cfg.exit_rel * b25)
            and (math.isnan(vals["hum"]) or "hum" not in st.base
                 or vals["hum"] < st.base["hum"] + cfg.exit_hum))
    trig = st.trigger_n + 1 if (over_pm or over_hum) else 0

    if st.phase in ("idle", "cooldown"):
        if trig >= cfg.enter_n:                     # bắt đầu sự kiện (tính từ mẫu vượt đầu tiên)
            start = ts - (cfg.enter_n - 1)
            seq = pre if len(pre) >= cfg.pre_s else (pre[:1] * (cfg.pre_s - len(pre)) + pre)
            st = TrackerState(phase="event", last_ts=ts, base=st.base, ring=ring, trigger_n=trig,
                              start_ts=start, acc=_accumulate(_new_acc(), vals, st.base, ring, cfg),
                              label=None, n_events=st.n_events + 1, pre=pre, seq=seq)
            return _maybe_classify(st, classify, cfg)
        if st.phase == "cooldown":
            if ts - st.cooldown_since >= cfg.cooldown_s:
                return _idle_after(dataclasses.replace(st, ring=ring, pre=pre), ts)
            return dataclasses.replace(st, last_ts=ts, ring=ring, pre=pre, trigger_n=trig)
        if trig:                                    # mẫu đang vượt ngưỡng: không học vào nền
            return dataclasses.replace(st, last_ts=ts, ring=ring, pre=pre, trigger_n=trig)
        alpha = 1.0 - math.exp(-1.0 / cfg.base_tau_s)
        return dataclasses.replace(st, last_ts=ts, ring=ring, pre=pre, trigger_n=trig,
                                   base=_update_base(st.base, vals, alpha))

    # phase == "event"
    acc = _accumulate(st.acc, vals, st.base, ring, cfg)
    calm_since = (st.calm_since if st.calm_since is not None else ts) if calm else None
    seq = st.seq + (row,) if len(st.seq) < cfg.pre_s + cfg.lock_after_s else st.seq
    st = dataclasses.replace(st, last_ts=ts, ring=ring, pre=pre, trigger_n=trig, acc=acc,
                             calm_since=calm_since, seq=seq)
    if ts - st.start_ts >= cfg.max_event_s:         # nền đã đổi hẳn: lấy nền mới
        return _idle_after(st, ts, base=_update_base({}, vals, 1.0))
    if calm_since is not None and ts - calm_since >= cfg.exit_hold_s:
        return dataclasses.replace(st, phase="cooldown", cooldown_since=ts, trigger_n=0)
    return _maybe_classify(st, classify, cfg)


def _maybe_classify(st: TrackerState, classify, cfg: EventCfg) -> TrackerState:
    if classify is None or st.locked or st.age_s is None or st.age_s < cfg.classify_from_s:
        return st
    label, conf = classify(st)
    return dataclasses.replace(st, label=label, confidence=None if conf is None else float(conf),
                               locked=label is not None and st.age_s >= cfg.lock_after_s)


def run(st: TrackerState, rows, classify=None, cfg: EventCfg = EventCfg()) -> TrackerState:
    """Chạy update() lần lượt trên [(ts, {kênh: giá trị})] (aiot.db.fast_rows)."""
    for ts, vals in rows:
        st = update(st, ts, vals, classify, cfg)
    return st
