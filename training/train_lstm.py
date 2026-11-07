"""
Huấn luyện LSTM dự báo PM2.5 6 giờ tới và xuất TFLite cho Pi (Task 12, chạy trên laptop).

Quy trình (theo "Pipeline AI theo Chương 12" trong CLAUDE.md):
- Dữ liệu: training/data/hanoi_openmeteo.csv (PM2.5 mô hình CAMS + thời tiết ERA5 của Hà
  Nội, xem SOURCE.md). Dữ liệu Pi còn quá ngắn nên chưa dùng.
- Chia theo thời gian 70/15/15 (chrono_split), cửa sổ 24 giờ -> 6 giờ trong từng tập
  (make_windows), không cửa sổ nào vắt qua hai tập.
- MinMaxScaler chỉ fit trên tập train; chuẩn hoá bằng đúng hàm numpy mà Pi dùng
  (aiot.forecast.scale_features) để tránh lệch giữa huấn luyện và suy luận.
- So sánh 4 cấu hình: đích là mức PM2.5 ('pm25') hoặc sai phân lag 24 ('diff24', đảo ngược
  bằng cách cộng giá trị cùng giờ hôm qua), có hoặc không có temp+hum. Chọn theo MAE
  trung bình 6 horizon trên tập validation; tập test KHÔNG được dùng ở đây (đánh giá
  đúng một lần trong 02_danh_gia.ipynb).
- LSTM 1 lớp, early stopping trên validation, seed cố định.
- Xuất .tflite batch 1, chỉ op có sẵn của TFLite (không Flex); kiểm tra Keras và .tflite
  lệch < 1e-3 trên 20 cửa sổ.

Chạy: .venv/Scripts/python -m training.train_lstm
Kết quả ở models/ (không commit): pm25_lstm.tflite, scaler.json, pm25_lstm.keras,
train_summary.json.

TensorFlow chỉ được nạp bên trong hàm, để test và notebook dùng các hàm còn lại mà không
cần TensorFlow.
"""
import argparse
import json
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from aiot import forecast as fc
from training.data import add_time_features, chrono_split, fit_scaler, make_windows
from training.evaluate import evaluate

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "training" / "data" / "hanoi_openmeteo.csv"
OUT = ROOT / "models"
TZ = "Asia/Ho_Chi_Minh"
SEED = 42
N_IN, N_OUT, LAG = fc.INPUT_HOURS, fc.HORIZONS, 24
ALL_FEATURES = ["pm25", "temp", "hum", "hour_sin", "hour_cos"]
NO_WEATHER = ["pm25", "hour_sin", "hour_cos"]
CONFIGS = [
    {"name": "pm25_thoitiet", "target": "pm25", "features": ALL_FEATURES},
    {"name": "pm25_khong_thoitiet", "target": "pm25", "features": NO_WEATHER},
    {"name": "diff24_thoitiet", "target": "diff24", "features": ALL_FEATURES},
    {"name": "diff24_khong_thoitiet", "target": "diff24", "features": NO_WEATHER},
]


# ---------------- dữ liệu ----------------
def load_frame(path=DATA) -> pd.DataFrame:
    """DataFrame theo giờ (giờ Hà Nội) với cột pm25, temp, hum, hour_sin, hour_cos."""
    df = pd.read_csv(path)
    idx = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(TZ)
    df = df.set_index(pd.DatetimeIndex(idx, name="timestamp"))[["pm25", "temp", "hum"]]
    return add_time_features(df.astype(float))


def split_windows(df: pd.DataFrame):
    """{'train'|'val'|'test': (X_raw, y_raw, target_times)} chưa chuẩn hoá, cột ALL_FEATURES,
    cùng các DataFrame của từng tập (để fit scaler trên train)."""
    parts = dict(zip(("train", "val", "test"), chrono_split(df[ALL_FEATURES], 0.7, 0.15)))
    windows = {k: make_windows(v, target="pm25", n_in=N_IN, n_out=N_OUT)
               for k, v in parts.items()}
    return parts, windows


def same_hour_yesterday(X_raw) -> np.ndarray:
    """PM2.5 ở giờ đích - 24 giờ cho từng horizon: X_raw[:, k - 1, pm25] (k = 1..6)."""
    return X_raw[:, :N_OUT, ALL_FEATURES.index("pm25")]


def window_features(X_raw) -> list[dict]:
    """Mỗi cửa sổ thành dict {đặc trưng: mảng 24 giờ} giống đầu vào predictor trên Pi."""
    return [{f: X_raw[i, :, j] for j, f in enumerate(ALL_FEATURES)} for i in range(len(X_raw))]


# ---------------- scaler.json ----------------
def scaler_spec(scaler, features, target_kind: str, target_scaler=None,
                model_version: str = "") -> dict:
    """Nội dung scaler.json (định dạng xem aiot.forecast.read_scaler_spec).

    scaler: MinMaxScaler fit trên train theo thứ tự `features`. target_scaler: MinMaxScaler
    một cột của đích (bắt buộc với 'diff24'); với 'pm25' bỏ trống thì dùng tham số cột pm25."""
    if target_kind not in fc.TARGET_KINDS:
        raise ValueError(f"target_kind phải thuộc {fc.TARGET_KINDS}")
    features = list(features)
    if target_scaler is not None:
        tmin, tmax = float(target_scaler.data_min_[0]), float(target_scaler.data_max_[0])
    elif target_kind == "pm25":
        j = features.index("pm25")
        tmin, tmax = float(scaler.data_min_[j]), float(scaler.data_max_[j])
    else:
        raise ValueError("đích diff24 cần target_scaler")
    return {
        "format": fc.SCALER_FORMAT,
        "model_version": model_version,
        "features": features,
        "data_min": [float(v) for v in scaler.data_min_],
        "data_max": [float(v) for v in scaler.data_max_],
        "target": {"kind": target_kind, "min": tmin, "max": tmax,
                   **({"lag": LAG} if target_kind == "diff24" else {})},
        "n_in": N_IN,
        "n_out": N_OUT,
    }


def make_spec(cfg: dict, parts, windows, model_version: str = "") -> dict:
    """scaler.json cho một cấu hình; mọi tham số chỉ lấy từ tập train."""
    from sklearn.preprocessing import MinMaxScaler

    scaler = fit_scaler(parts["train"], cfg["features"])
    target_scaler = None
    if cfg["target"] == "diff24":
        X_tr, y_tr, _ = windows["train"]
        d = (y_tr - same_hour_yesterday(X_tr)).reshape(-1, 1)
        target_scaler = MinMaxScaler().fit(d)
    return scaler_spec(scaler, cfg["features"], cfg["target"], target_scaler, model_version)


def to_model_space(spec_raw: dict, X_raw, y_raw=None):
    """(X chuẩn hoá float32 (n, 24, số đặc trưng), y chuẩn hoá hoặc None)."""
    spec = _parsed(spec_raw)
    idx = [ALL_FEATURES.index(f) for f in spec["features"]]
    X = fc.scale_features(spec, X_raw[..., idx]).astype(np.float32)
    if y_raw is None:
        return X, None
    y = y_raw - same_hour_yesterday(X_raw) if spec["target_kind"] == "diff24" else y_raw
    rng = float(fc._range(spec["target_min"], spec["target_max"]))
    return X, ((y - spec["target_min"]) / rng).astype(np.float32)


def from_model_space(spec_raw: dict, y_scaled, X_raw) -> np.ndarray:
    """Đầu ra mô hình (n, 6) -> PM2.5 µg/m³, đảo ngược như predictor trên Pi."""
    spec = _parsed(spec_raw)
    y = fc.unscale_target(spec, y_scaled)
    if spec["target_kind"] == "diff24":
        y = y + same_hour_yesterday(X_raw)
    return y


def _parsed(spec_raw: dict) -> dict:
    """Kiểm tra spec bằng đúng hàm đọc scaler.json của Pi."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "scaler.json"
        p.write_text(json.dumps(spec_raw), encoding="utf-8")
        return fc.read_scaler_spec(p)


# ---------------- mô hình ----------------
def build_model(n_in: int = N_IN, n_feat: int = len(ALL_FEATURES), units: int = 64,
                n_out: int = N_OUT, lr: float = 1e-3):
    """LSTM 1 lớp -> Dense(n_out), loss MSE, tối ưu Adam."""
    import tensorflow as tf
    model = tf.keras.Sequential([
        tf.keras.Input(shape=(n_in, n_feat)),
        tf.keras.layers.LSTM(units),
        tf.keras.layers.Dense(n_out),
    ])
    model.compile(optimizer=tf.keras.optimizers.Adam(lr), loss="mse", metrics=["mae"])
    return model


def export_tflite(model, path, n_in: int = N_IN, n_feat: int = len(ALL_FEATURES)) -> int:
    """Ghi .tflite với đầu vào cố định (1, n_in, n_feat) float32, chỉ op có sẵn của TFLite
    (không SELECT_TF_OPS/Flex). Trả về kích thước file (byte).

    Dựng lại mô hình với batch cố định 1 rồi chép trọng số, chuyển bằng from_keras_model.
    (Với Keras 3, from_concrete_functions không đóng băng biến: file có VAR_HANDLE /
    READ_VARIABLE chưa khởi tạo, chạy ra NaN hoặc lỗi; from_keras_model đóng băng đúng.)"""
    import tensorflow as tf

    lstm = next(l for l in model.layers if isinstance(l, tf.keras.layers.LSTM))
    dense = model.layers[-1]
    fixed = tf.keras.Sequential([
        tf.keras.Input(shape=(n_in, n_feat), batch_size=1),
        tf.keras.layers.LSTM(lstm.units),
        tf.keras.layers.Dense(dense.units),
    ])
    fixed.set_weights(model.get_weights())

    conv = tf.lite.TFLiteConverter.from_keras_model(fixed)
    conv.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS]
    data = conv.convert()
    if b"Flex" in data:
        raise RuntimeError("mô hình TFLite có op Flex, Pi không chạy được")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


def tflite_predict(path, X) -> np.ndarray:
    """Chạy .tflite từng cửa sổ (batch 1) bằng Interpreter mà Pi dùng (hoặc tensorflow.lite)."""
    interp = fc._interpreter_class()(model_path=str(path))
    interp.allocate_tensors()
    i, o = interp.get_input_details()[0]["index"], interp.get_output_details()[0]["index"]
    out = []
    for x in np.asarray(X, dtype=np.float32):
        interp.set_tensor(i, x[None])
        interp.invoke()
        out.append(np.array(interp.get_tensor(o))[0])
    return np.stack(out)


def parity(model, tflite_path, X, n: int = 20) -> float:
    """Sai lệch tuyệt đối lớn nhất (đơn vị chuẩn hoá) giữa Keras và .tflite trên n cửa sổ."""
    X = np.asarray(X[:n], dtype=np.float32)
    keras_out = model.predict(X, batch_size=1, verbose=0)
    return float(np.abs(keras_out - tflite_predict(tflite_path, X)).max())


def train_config(cfg: dict, parts, windows, epochs: int, patience: int, batch: int,
                 units: int, seed: int = SEED):
    """Huấn luyện một cấu hình; trả về (model, spec, kết quả validation)."""
    import tensorflow as tf
    tf.keras.utils.set_random_seed(seed)
    spec = make_spec(cfg, parts, windows)
    X_tr, y_tr = to_model_space(spec, *windows["train"][:2])
    X_va_raw, y_va_raw, _ = windows["val"]
    X_va, y_va = to_model_space(spec, X_va_raw, y_va_raw)
    model = build_model(N_IN, len(cfg["features"]), units)
    stop = tf.keras.callbacks.EarlyStopping(monitor="val_loss", patience=patience,
                                            restore_best_weights=True)
    t0 = time.perf_counter()
    hist = model.fit(X_tr, y_tr, validation_data=(X_va, y_va), epochs=epochs,
                     batch_size=batch, shuffle=True, callbacks=[stop], verbose=0)
    secs = time.perf_counter() - t0
    pred = from_model_space(spec, model.predict(X_va, batch_size=1024, verbose=0), X_va_raw)
    metrics = evaluate(y_va_raw, pred)
    result = {
        "name": cfg["name"], "target": cfg["target"], "features": cfg["features"],
        "epochs_run": len(hist.history["loss"]),
        "best_epoch": int(np.argmin(hist.history["val_loss"])) + 1,
        "train_seconds": round(secs, 1),
        "val_mae_mean": float(np.mean(metrics["mae"])),
        "val": metrics,
    }
    return model, spec, result


# ---------------- đối chứng hồi quy ----------------
def lag_features(X_raw) -> np.ndarray:
    """Đặc trưng trễ cho hồi quy: 24 giờ PM2.5, temp/hum giờ cuối, sin/cos giờ cuối."""
    j = {f: i for i, f in enumerate(ALL_FEATURES)}
    return np.column_stack([X_raw[:, :, j["pm25"]], X_raw[:, -1, j["temp"]],
                            X_raw[:, -1, j["hum"]], X_raw[:, -1, j["hour_sin"]],
                            X_raw[:, -1, j["hour_cos"]]])


def fit_ridge(windows, alphas=(0.01, 0.1, 1.0, 10.0, 100.0, 1000.0)):
    """Ridge nhiều đầu ra trên đặc trưng trễ (chuẩn hoá theo train), alpha chọn theo MAE
    validation. Trả về (pipeline, alpha, {alpha: MAE TB validation})."""
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    X_tr, y_tr, _ = windows["train"]
    X_va, y_va, _ = windows["val"]
    scores = {}
    for a in alphas:
        m = make_pipeline(StandardScaler(), Ridge(alpha=a)).fit(lag_features(X_tr), y_tr)
        scores[a] = float(np.mean(evaluate(y_va, m.predict(lag_features(X_va)))["mae"]))
    best = min(scores, key=scores.get)
    model = make_pipeline(StandardScaler(), Ridge(alpha=best)).fit(lag_features(X_tr), y_tr)
    return model, best, scores


# ---------------- chạy ----------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Huấn luyện LSTM PM2.5 và xuất TFLite")
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--epochs", type=int, default=70)
    parser.add_argument("--patience", type=int, default=5)
    parser.add_argument("--batch", type=int, default=256)
    parser.add_argument("--units", type=int, default=64)
    args = parser.parse_args(argv)
    import tensorflow as tf

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    df = load_frame(args.data)
    parts, windows = split_windows(df)
    for k in ("train", "val", "test"):
        p = parts[k]
        print(f"{k}: {p.index[0]} -> {p.index[-1]}, {len(p)} giờ, {len(windows[k][0])} cửa sổ")

    t_all = time.perf_counter()
    results, models = [], {}
    for cfg in CONFIGS:
        model, spec, res = train_config(cfg, parts, windows, args.epochs, args.patience,
                                        args.batch, args.units)
        results.append(res)
        models[cfg["name"]] = (model, spec)
        print(f"{res['name']:24s} MAE val TB {res['val_mae_mean']:.3f}  "
              f"epoch {res['best_epoch']}/{res['epochs_run']}  {res['train_seconds']} s")

    best = min(results, key=lambda r: r["val_mae_mean"])
    model, spec = models[best["name"]]
    spec["model_version"] = f"pm25_lstm-{date.today().isoformat()}-{best['name']}"
    print(f"Chọn theo MAE validation: {best['name']}")

    tfl_path = out / "pm25_lstm.tflite"
    size = export_tflite(model, tfl_path, N_IN, len(spec["features"]))
    (out / "scaler.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    model.save(out / "pm25_lstm.keras")
    X_va, _ = to_model_space(spec, windows["val"][0])
    diff = parity(model, tfl_path, X_va, n=20)
    print(f"TFLite {size} byte; lệch Keras/TFLite lớn nhất trên 20 cửa sổ: {diff:.2e}")
    if diff >= 1e-3:
        raise SystemExit(f"Keras và TFLite lệch {diff} >= 1e-3")

    # Đối chứng trên validation để tham khảo (không dùng để chọn LSTM)
    X_va_raw, y_va_raw, _ = windows["val"]
    ridge, alpha, ridge_scores = fit_ridge(windows)
    val_baselines = {
        "persistence": evaluate(y_va_raw, np.repeat(X_va_raw[:, -1:, 0], N_OUT, axis=1)),
        "seasonal_naive": evaluate(y_va_raw, same_hour_yesterday(X_va_raw)),
        "ridge": evaluate(y_va_raw, ridge.predict(lag_features(X_va_raw))),
    }
    summary = {
        "trained_on": date.today().isoformat(),
        "tensorflow": tf.__version__,
        "seed": SEED,
        "hyper": {"units": args.units, "epochs": args.epochs, "patience": args.patience,
                  "batch": args.batch, "loss": "mse", "optimizer": "adam 1e-3"},
        "split": {k: {"start": str(parts[k].index[0]), "end": str(parts[k].index[-1]),
                      "hours": len(parts[k]), "windows": len(windows[k][0])}
                  for k in ("train", "val", "test")},
        "configs": results,
        "chosen": best["name"],
        "model_version": spec["model_version"],
        "tflite_bytes": size,
        "parity_max_abs_scaled": diff,
        "ridge_alpha": alpha,
        "ridge_val_scores": {str(k): v for k, v in ridge_scores.items()},
        "val_baselines": val_baselines,
        "total_seconds": round(time.perf_counter() - t_all, 1),
    }
    (out / "train_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    print(f"Tổng thời gian: {summary['total_seconds']} s. Đã ghi {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
