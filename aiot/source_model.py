"""
Bộ nhận diện nguồn bụi (chạy trong controller mỗi control.loop_s giây): logistic regression đa lớp
trên đặc trưng cửa sổ 120 s so với nền phòng (aiot/source_window.py), suy luận bằng numpy (không
cần scikit-learn trên Pi). Mô hình do training/train_logreg.py huấn luyện trên dữ liệu sinh từ số
liệu thật (training/demo_data.py), lưu ở models/source_logreg.json.

3 nhãn: normal (bình thường), cooking (nấu ăn: bụi + ẩm/nhiệt cùng tăng), pollution (ô nhiễm bụi
mịn: bụi tăng mà ẩm, nhiệt không đổi; gộp "ô nhiễm từ ngoài vào" và "khói thuốc/nhang" cũ).
Điểm bất thường = 1 − P(normal).

File mô hình JSON, định dạng FORMAT:
  {"format", "name", "classes", "features", "mean", "scale", "coef" (K×D), "intercept" (K), "meta"}
Xác suất = softmax(coef · ((x − mean) / scale) + intercept), giống sklearn
StandardScaler + LogisticRegression (multinomial).

smooth(): làm mượt theo thời gian — trung bình xác suất vote_n lần dự đoán gần nhất (5 giây/lần),
chỉ đổi nhãn khi xác suất trung bình của nhãn mới >= min_confidence. Hàm thuần, trạng thái
SourceState lưu ở khoá state 'source_state' (JSON).

Bảng hành động (cố định, không do mô hình quyết định):
  normal: quạt tắt, còi im · cooking: quạt bật · pollution: quạt bật + còi
"""
import dataclasses
import json
from dataclasses import dataclass, field

import numpy as np


from aiot.source_window import WINDOW_FEATURES, compute_window_features

LABELS = ("normal", "cooking", "pollution")
FAN_LABELS = frozenset({"cooking", "pollution"})
BUZZER_LABELS = frozenset({"pollution"})
NAMES_VI = {"normal": "bình thường", "cooking": "nấu ăn", "pollution": "ô nhiễm bụi mịn"}
FORMAT = "aiot-logreg-v1"


class LogRegModel:
    def __init__(self, spec: dict):
        if spec.get("format") != FORMAT:
            raise ValueError(f"mô hình không đúng định dạng {FORMAT}")
        if tuple(spec["features"]) != WINDOW_FEATURES:
            raise ValueError("danh sách đặc trưng của mô hình khác aiot.source_window.WINDOW_FEATURES")
        self.classes = tuple(spec["classes"])
        if set(self.classes) != set(LABELS) or len(self.classes) != len(LABELS):
            raise ValueError(f"nhãn của mô hình phải là {LABELS}")
        d, k = len(WINDOW_FEATURES), len(self.classes)
        self.features = WINDOW_FEATURES
        self.name = spec.get("name", "source_logreg")
        self.meta = spec.get("meta", {})
        self.mean = np.asarray(spec["mean"], dtype=float)
        scale = np.asarray(spec["scale"], dtype=float)
        self.coef = np.asarray(spec["coef"], dtype=float)
        self.intercept = np.asarray(spec["intercept"], dtype=float)
        if self.mean.shape != (d,) or scale.shape != (d,):
            raise ValueError("mean/scale không khớp số đặc trưng")
        if self.coef.shape != (k, d) or self.intercept.shape != (k,):
            raise ValueError("coef/intercept không khớp số nhãn và số đặc trưng")
        self.scale = np.where(scale > 1e-9, scale, 1.0)

    def predict_proba_batch(self, X) -> np.ndarray:
        Z = (np.asarray(X, dtype=float) - self.mean) / self.scale
        s = Z @ self.coef.T + self.intercept
        s -= s.max(axis=1, keepdims=True)
        e = np.exp(s)
        return e / e.sum(axis=1, keepdims=True)

    def predict_proba(self, x) -> np.ndarray:
        """Xác suất theo thứ tự self.classes cho một vector đặc trưng."""
        return self.predict_proba_batch(np.asarray(x, dtype=float)[None, :])[0]

    def predict(self, x) -> tuple[str, dict]:
        p = self.predict_proba(x)
        return self.classes[int(np.argmax(p))], {c: float(v) for c, v in zip(self.classes, p)}

    def featurize(self, buf: dict):
        return compute_window_features(buf)

    def anomaly_score(self, p) -> float:
        return float(1.0 - p[self.classes.index("normal")])

    def to_spec(self) -> dict:
        return {"format": FORMAT, "name": self.name, "classes": list(self.classes),
                "features": list(self.features), "mean": self.mean.tolist(),
                "scale": self.scale.tolist(), "coef": self.coef.tolist(),
                "intercept": self.intercept.tolist(), "meta": self.meta}


def spec_from_sklearn(scaler, lr, classes, name="source_logreg", meta=None) -> dict:
    """Spec JSON từ StandardScaler + LogisticRegression đã fit (classes: nhãn theo lr.classes_)."""
    return {"format": FORMAT, "name": name, "classes": list(classes),
            "features": list(WINDOW_FEATURES), "mean": scaler.mean_.tolist(),
            "scale": scaler.scale_.tolist(), "coef": lr.coef_.tolist(),
            "intercept": lr.intercept_.tolist(), "meta": meta or {}}


def load_model(path) -> LogRegModel:
    with open(path, encoding="utf-8") as f:
        return LogRegModel(json.load(f))


def save_model(model: LogRegModel, path) -> None:
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
