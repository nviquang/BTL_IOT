"""
Suy luận bộ nhận diện nguồn bụi trên Pi: rừng cây (RandomForest) xuất ra JSON, chỉ cần numpy.

- load_model(): đọc models/source_rf.json (training/train_source.py tạo), kiểm tra danh
  sách đặc trưng khớp aiot.source_features.FEATURES.
- SourceModel.predict_proba(x): trung bình xác suất lá của các cây, giống sklearn.
- smooth(): làm mượt theo thời gian — trung bình xác suất vote_n lần dự đoán gần nhất
  (5 giây/lần), chỉ đổi nhãn khi xác suất trung bình của nhãn mới >= min_confidence.
  Hàm thuần, trạng thái SourceState lưu ở khoá state 'source_state' (JSON).

Bảng hành động (cố định, không do mô hình quyết định):
  normal: quạt tắt, còi im · cooking: quạt bật · pollution: quạt bật + còi · smoking: còi
"""
import dataclasses
import json
from dataclasses import dataclass, field

import numpy as np

from aiot.source_features import FEATURES

LABELS = ("normal", "cooking", "pollution", "smoking")
FAN_LABELS = frozenset({"cooking", "pollution"})
BUZZER_LABELS = frozenset({"pollution", "smoking"})
FORMAT = "aiot-rf-v1"
NAMES_VI = {"normal": "bình thường", "cooking": "nấu ăn",
            "pollution": "ô nhiễm từ ngoài vào", "smoking": "khói thuốc/nhang"}


class SourceModel:
    def __init__(self, spec: dict):
        if spec.get("format") != FORMAT:
            raise ValueError(f"mô hình không đúng định dạng {FORMAT}")
        if tuple(spec["features"]) != FEATURES:
            raise ValueError("danh sách đặc trưng của mô hình khác aiot.source_features.FEATURES")
        self.classes = tuple(spec["classes"])
        if set(self.classes) != set(LABELS):
            raise ValueError(f"nhãn của mô hình phải là {LABELS}")
        self.name = spec.get("name", "source_rf")
        self.trees = []
        for t in spec["trees"]:
            value = np.asarray(t["value"], dtype=float)
            value = value / np.maximum(value.sum(axis=1, keepdims=True), 1e-12)
            self.trees.append((np.asarray(t["left"], dtype=np.int64),
                               np.asarray(t["right"], dtype=np.int64),
                               np.asarray(t["feature"], dtype=np.int64),
                               np.asarray(t["threshold"], dtype=float), value))
        if not self.trees:
            raise ValueError("mô hình không có cây nào")

    def predict_proba(self, x) -> np.ndarray:
        """Xác suất theo thứ tự self.classes cho một vector đặc trưng."""
        # sklearn so sánh đặc trưng đã ép về float32 với ngưỡng: làm y hệt để khớp từng lá
        x = np.asarray(x, dtype=np.float32).astype(float)
        out = np.zeros(len(self.classes))
        for left, right, feat, thr, value in self.trees:
            node = 0
            while left[node] != -1:
                node = left[node] if x[feat[node]] <= thr[node] else right[node]
            out += value[node]
        return out / len(self.trees)

    def predict_proba_batch(self, X) -> np.ndarray:
        """Xác suất cho nhiều vector (huấn luyện/đánh giá trên laptop)."""
        X = np.asarray(X, dtype=np.float32).astype(float)
        out = np.zeros((len(X), len(self.classes)))
        rows = np.arange(len(X))
        for left, right, feat, thr, value in self.trees:
            node = np.zeros(len(X), dtype=np.int64)
            inner = left[node] != -1
            while inner.any():
                n = node[inner]
                go_left = X[rows[inner], feat[n]] <= thr[n]
                node[inner] = np.where(go_left, left[n], right[n])
                inner = left[node] != -1
            out += value[node]
        return out / len(self.trees)

    def predict(self, x) -> tuple[str, dict]:
        p = self.predict_proba(x)
        return self.classes[int(np.argmax(p))], {c: float(v) for c, v in zip(self.classes, p)}


def load_model(path) -> SourceModel:
    with open(path, encoding="utf-8") as f:
        return SourceModel(json.load(f))


@dataclass(frozen=True)
class SourceState:
    label: str | None = None            # nhãn đã làm mượt; None khi chưa đủ tin cậy
    since: int | None = None            # lúc chuyển sang nhãn hiện tại
    last_ts: int | None = None          # lần dự đoán gần nhất
    history: tuple = field(default_factory=tuple)   # xác suất vote_n lần gần nhất

    def to_json(self):
        d = dataclasses.asdict(self)
        d["history"] = [list(h) for h in self.history]
        return json.dumps(d, sort_keys=True)

    @classmethod
    def from_json(cls, text):
        """Đọc lại; thiếu/hỏng thì trạng thái rỗng (không có nhãn)."""
        try:
            d = json.loads(text)
            label = d["label"] if d["label"] in LABELS else None
            hist = tuple(tuple(float(v) for v in h) for h in d["history"]
                         if len(h) == len(LABELS))
            since = d["since"] if isinstance(d["since"], int) else None
            last = d["last_ts"] if isinstance(d["last_ts"], int) else None
            return cls(label=label, since=since, last_ts=last, history=hist)
        except (TypeError, ValueError, KeyError):
            return cls()


def smooth(st: SourceState, proba, now: int, *, vote_n: int = 3, min_confidence: float = 0.6,
           max_gap_s: int = 30, classes=LABELS) -> SourceState:
    """Một bước làm mượt. proba: xác suất theo classes, hoặc None khi thiếu dữ liệu.

    Thiếu dữ liệu hoặc ngắt quãng quá max_gap_s: xoá lịch sử và nhãn (không quyết định
    theo dự đoán cũ)."""
    now = int(now)
    if proba is None:
        return SourceState()
    order = [classes.index(c) for c in LABELS]
    p = tuple(float(proba[i]) for i in order)
    hist = st.history if st.last_ts is not None and now - st.last_ts <= max_gap_s else ()
    label, since = (st.label, st.since) if hist else (None, None)
    hist = (hist + (p,))[-max(1, vote_n):]
    mean = np.mean(np.asarray(hist), axis=0)
    cand = LABELS[int(np.argmax(mean))]
    if cand != label and mean[LABELS.index(cand)] >= min_confidence:
        label, since = cand, now
    return SourceState(label=label, since=since, last_ts=now, history=hist)


def actions(label: str | None) -> tuple[bool | None, bool | None]:
    """(quạt, còi) theo bảng hành động; (None, None) khi không có nhãn."""
    if label is None:
        return None, None
    return label in FAN_LABELS, label in BUZZER_LABELS
