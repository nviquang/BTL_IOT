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
FORMAT_KNN = "aiot-knn-v1"
FORMAT_SHAPE = "aiot-shape-knn-v1"
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


class KnnModel:
    """kNN trên đặc trưng sự kiện (aiot.source_event.EVENT_FEATURES), cho profile demo.

    Mô hình chính là bảng mẫu thật đã gán nhãn; chuẩn hoá theo mean/std lưu kèm. Mẫu gần
    nhất xa hơn max_dist (đơn vị đã chuẩn hoá) thì trả nhãn None ("chưa gặp kiểu này")."""

    def __init__(self, spec: dict):
        from aiot.source_event import EVENT_FEATURES
        if spec.get("format") != FORMAT_KNN:
            raise ValueError(f"mô hình không đúng định dạng {FORMAT_KNN}")
        if tuple(spec["features"]) != EVENT_FEATURES:
            raise ValueError("danh sách đặc trưng của mô hình khác aiot.source_event.EVENT_FEATURES")
        self.classes = LABELS
        self.features = EVENT_FEATURES
        self.name = spec.get("name", "source_knn")
        self.k = int(spec.get("k", 3))
        self.max_dist = float(spec["max_dist"]) if spec.get("max_dist") is not None else None
        self.mean = np.asarray(spec["mean"], dtype=float)
        std = np.asarray(spec["std"], dtype=float)
        self.std = np.where(std > 1e-9, std, 1.0)
        self.X = np.asarray(spec["X"], dtype=float).reshape(-1, len(EVENT_FEATURES))
        self.y = tuple(spec["y"])
        self.meta = spec.get("meta", {})
        if len(self.y) != len(self.X) or not self.y:
            raise ValueError("mô hình kNN cần ít nhất một mẫu, X và y cùng độ dài")
        if not set(self.y) <= set(LABELS):
            raise ValueError(f"nhãn của mô hình phải thuộc {LABELS}")
        self._Z = (self.X - self.mean) / self.std

    @classmethod
    def fit(cls, X, y, *, k: int = 3, max_dist: float | None = None, name="source_knn",
            meta=None) -> "KnnModel":
        from aiot.source_event import EVENT_FEATURES
        X = np.asarray(X, dtype=float)
        return cls({"format": FORMAT_KNN, "name": name, "features": list(EVENT_FEATURES),
                    "k": k, "max_dist": max_dist, "mean": X.mean(axis=0).tolist(),
                    "std": X.std(axis=0).tolist(), "X": X.tolist(), "y": list(y),
                    "meta": meta or {}})

    def to_spec(self) -> dict:
        return {"format": FORMAT_KNN, "name": self.name, "features": list(self.features),
                "k": self.k, "max_dist": self.max_dist, "mean": self.mean.tolist(),
                "std": self.std.tolist(), "X": self.X.tolist(), "y": list(self.y),
                "meta": self.meta}

    def add_example(self, x, label: str) -> "KnnModel":
        """Mô hình mới có thêm một mẫu (lệnh /teach); giữ nguyên mean/std để không xê dịch
        các mẫu cũ."""
        if label not in LABELS:
            raise ValueError(f"nhãn phải là một trong {LABELS}")
        spec = self.to_spec()
        spec["X"] = self.X.tolist() + [np.asarray(x, dtype=float).tolist()]
        spec["y"] = list(self.y) + [label]
        return KnnModel(spec)

    def neighbors(self, x):
        """[(chỉ số mẫu, khoảng cách)] của k mẫu gần nhất, tăng dần theo khoảng cách."""
        z = (np.asarray(x, dtype=float) - self.mean) / self.std
        d = np.sqrt(((self._Z - z) ** 2).sum(axis=1))
        idx = np.argsort(d, kind="stable")[:max(1, min(self.k, len(d)))]
        return [(int(i), float(d[i])) for i in idx]

    def predict_proba(self, x) -> np.ndarray:
        """Tỉ lệ phiếu của k láng giềng theo thứ tự LABELS."""
        out = np.zeros(len(LABELS))
        nb = self.neighbors(x)
        for i, _ in nb:
            out[LABELS.index(self.y[i])] += 1.0
        return out / len(nb)

    def predict(self, x) -> tuple[str | None, dict]:
        """(nhãn | None, thông tin). Đa số phiếu; hoà thì theo mẫu gần nhất. Xa mọi mẫu: None."""
        nb = self.neighbors(x)
        votes = {}
        for i, _ in nb:
            votes[self.y[i]] = votes.get(self.y[i], 0) + 1
        top = max(votes.values())
        label = next(self.y[i] for i, _ in nb if votes[self.y[i]] == top)
        info = {"votes": votes, "confidence": top / len(nb), "nearest_dist": nb[0][1],
                "neighbors": [{"index": i, "label": self.y[i], "dist": round(d, 3)} for i, d in nb]}
        if self.max_dist is not None and nb[0][1] > self.max_dist:
            info["unknown"] = True
            return None, info
        return label, info

    def classify(self, x) -> tuple[str | None, float | None]:
        """Dạng dùng cho aiot.source_event.update(classify=...)."""
        if x is None:
            return None, None
        label, info = self.predict(x)
        return label, info["confidence"]

    def classify_event(self, st):
        """Dạng dùng cho aiot.source_event.update(classify=...): trạng thái -> (nhãn, p)."""
        from aiot.source_event import features
        return self.classify(features(st))


def dtw(a, b, band: int = 5) -> float:
    """Khoảng cách DTW nhiều kênh (Euclid giữa hai hàng), cửa sổ Sakoe-Chiba ±band,
    chia cho số hàng dài hơn để so được các độ dài khác nhau."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return float("inf")
    w = max(band, abs(n - m))
    cost = np.sqrt(((a[:, None, :] - b[None, :, :]) ** 2).sum(axis=2))
    D = np.full((n + 1, m + 1), np.inf)
    D[0, 0] = 0.0
    for i in range(1, n + 1):
        lo, hi = max(1, i - w), min(m, i + w)
        prev, diag = D[i - 1], D[i - 1]
        row = D[i]
        for j in range(lo, hi + 1):
            row[j] = cost[i - 1, j - 1] + min(prev[j], row[j - 1], diag[j - 1])
    return float(D[n, m] / max(n, m))


class ShapeKnnModel:
    """kNN-DTW trên ĐƯỜNG CONG sự kiện (aiot.source_event.shape): mỗi mẫu học là cả cửa sổ
    nhiều kênh của một lần làm đạo cụ, không phải vài số tóm tắt.

    Lúc sự kiện mới được t giây, chỉ so với t giây đầu của các mẫu học (phân loại sớm). Mỗi
    kênh chia cho độ lệch chuẩn của nó trên tập học để các kênh nặng như nhau."""

    def __init__(self, spec: dict):
        from aiot.source_event import SHAPE_CHANNELS
        if spec.get("format") != FORMAT_SHAPE:
            raise ValueError(f"mô hình không đúng định dạng {FORMAT_SHAPE}")
        if tuple(spec["channels"]) != SHAPE_CHANNELS:
            raise ValueError("kênh của mô hình khác aiot.source_event.SHAPE_CHANNELS")
        self.classes = LABELS
        self.channels = SHAPE_CHANNELS
        self.name = spec.get("name", "source_shape_knn")
        self.k = int(spec.get("k", 1))
        self.band = int(spec.get("band", 5))
        self.max_dist = float(spec["max_dist"]) if spec.get("max_dist") is not None else None
        scale = np.asarray(spec["scale"], dtype=float)
        self.scale = np.where(scale > 1e-6, scale, 1.0)
        self.seqs = [np.asarray(q, dtype=float).reshape(-1, len(SHAPE_CHANNELS)) for q in spec["seqs"]]
        self.y = tuple(spec["y"])
        self.meta = spec.get("meta", {})
        if len(self.y) != len(self.seqs) or not self.y:
            raise ValueError("mô hình cần ít nhất một đường cong, seqs và y cùng độ dài")
        if not set(self.y) <= set(LABELS):
            raise ValueError(f"nhãn của mô hình phải thuộc {LABELS}")
        self._Z = [q / self.scale for q in self.seqs]

    @classmethod
    def fit(cls, seqs, y, *, k: int = 1, band: int = 5, max_dist: float | None = None,
            name="source_shape_knn", meta=None) -> "ShapeKnnModel":
        from aiot.source_event import SHAPE_CHANNELS
        seqs = [np.asarray(q, dtype=float) for q in seqs]
        allr = np.vstack(seqs)
        return cls({"format": FORMAT_SHAPE, "name": name, "channels": list(SHAPE_CHANNELS),
                    "k": k, "band": band, "max_dist": max_dist, "scale": allr.std(axis=0).tolist(),
                    "seqs": [q.tolist() for q in seqs], "y": list(y), "meta": meta or {}})

    def to_spec(self) -> dict:
        return {"format": FORMAT_SHAPE, "name": self.name, "channels": list(self.channels),
                "k": self.k, "band": self.band, "max_dist": self.max_dist,
                "scale": self.scale.tolist(), "seqs": [q.tolist() for q in self.seqs],
                "y": list(self.y), "meta": self.meta}

    def add_example(self, seq, label: str) -> "ShapeKnnModel":
        """Thêm một đường cong (lệnh /teach); giữ nguyên thang chia của các kênh."""
        if label not in LABELS:
            raise ValueError(f"nhãn phải là một trong {LABELS}")
        spec = self.to_spec()
        spec["seqs"].append(np.asarray(seq, dtype=float).tolist())
        spec["y"].append(label)
        return ShapeKnnModel(spec)

    def neighbors(self, seq):
        z = np.asarray(seq, dtype=float) / self.scale
        n = len(z)
        d = np.array([dtw(z[:min(n, len(q))], q[:n], self.band) for q in self._Z])
        idx = np.argsort(d, kind="stable")[:max(1, min(self.k, len(d)))]
        return [(int(i), float(d[i])) for i in idx]

    def predict_proba(self, seq) -> np.ndarray:
        out = np.zeros(len(LABELS))
        nb = self.neighbors(seq)
        for i, _ in nb:
            out[LABELS.index(self.y[i])] += 1.0
        return out / len(nb)

    def predict(self, seq) -> tuple[str | None, dict]:
        nb = self.neighbors(seq)
        votes = {}
        for i, _ in nb:
            votes[self.y[i]] = votes.get(self.y[i], 0) + 1
        top = max(votes.values())
        label = next(self.y[i] for i, _ in nb if votes[self.y[i]] == top)
        info = {"votes": votes, "confidence": top / len(nb), "nearest_dist": nb[0][1],
                "neighbors": [{"index": i, "label": self.y[i], "dist": round(d, 3)} for i, d in nb]}
        if self.max_dist is not None and nb[0][1] > self.max_dist:
            info["unknown"] = True
            return None, info
        return label, info

    def classify_event(self, st):
        from aiot.source_event import shape
        seq = shape(st)
        if seq is None:
            return None, None
        label, info = self.predict(seq)
        return label, info["confidence"]


def load_model(path):
    """SourceModel (aiot-rf-v1), KnnModel (aiot-knn-v1) hoặc ShapeKnnModel (aiot-shape-knn-v1)."""
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    if spec.get("format") == FORMAT_KNN:
        return KnnModel(spec)
    if spec.get("format") == FORMAT_SHAPE:
        return ShapeKnnModel(spec)
    return SourceModel(spec)


def save_model(model, path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(model.to_spec(), f, ensure_ascii=False)


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
