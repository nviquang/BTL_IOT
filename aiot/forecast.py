"""
Dự báo PM2.5 trung bình giờ cho 6 giờ tới: LSTM (TFLite, Task 12) nếu có, ngược lại mô
hình đối chứng (Task 9).

- Chỉ dùng thư viện chuẩn và numpy (Pi không cài pandas); numpy chỉ được nạp khi cần,
  nên controller và bot import module này mà không tốn thêm thời gian khởi động.
- Chuỗi giờ lấy từ bảng readings (TB 5 phút): giờ nào có ít nhất 6/12 cửa sổ hợp lệ thì
  lấy trung bình, ngược lại là None. Mốc giờ là epoch chia hết cho 3600.
- Lấy 48 giờ, dùng 24 giờ cuối làm đầu vào; phần trước chỉ để nội suy khoảng trống ở đầu
  cửa sổ từ hàng xóm bên trái (không lấy giá trị biên), để seasonal_naive dùng đúng giờ
  đích - 24 giờ.
- made_at = đầu giờ hiện tại; horizon k (1..6) là giờ thứ k sau giờ đủ cuối cùng, tức
  giờ bắt đầu lúc made_at + (k - 1) * 3600.
- Mô hình: 'seasonal_naive' (cùng giờ hôm qua) khi có đủ 24 giờ đầu vào sau nội suy,
  ngược lại 'persistence' (giá trị giờ hợp lệ gần nhất lặp lại 6 lần). predictor (LSTM)
  nếu có thì dùng trước, model 'lstm'; lỗi hoặc kết quả sai thì về đối chứng.
- predictor nhận dict các mảng 24 giờ: 'pm25', 'temp', 'hum' (cùng quy tắc 6/12 cửa sổ và
  nội suy như PM2.5) và 'hour_sin', 'hour_cos' (giờ trong ngày theo giờ địa phương của Pi,
  của từng giờ đầu vào). temp/hum không dựng được thì không có trong dict; predictor cần
  chúng sẽ lỗi và run_forecast về đối chứng.
- load_predictor(model_path, scaler_path) nạp .tflite bằng LiteRT (ai_edge_litert, hoặc
  tflite_runtime / tensorflow.lite nếu có) và scaler.json; thiếu file, file hỏng hay không
  có thư viện thì trả None (dùng đối chứng). Xoá file mô hình thì lần sau về đối chứng.
- Giờ hợp lệ mới nhất đã kết thúc quá 3 giờ: không dự báo, không ghi gì, không raise.
- Ghi 6 dòng bằng INSERT OR REPLACE trong một transaction (chạy lại cùng giờ thì ghi đè).
- main() bỏ qua lần chạy (thoát 0) khi đồng hồ chưa đồng bộ NTP, để lần chạy bù lúc khởi
  động không ghi dự báo với giờ sai.

Chạy một lần: cd ~/BTL_IOT && venv/bin/python -m aiot.forecast (timer mỗi giờ, phút 02).
"""
import argparse
import json
import logging
import math
import time
from pathlib import Path
from typing import Callable

from aiot.collector import is_clock_synced

log = logging.getLogger("aiot.forecast")

HOUR_S = 3600
HORIZONS = 6
WINDOWS_PER_HOUR = HOUR_S // 300        # 12 cửa sổ 5 phút
MIN_WINDOWS = 6                         # giờ cần ít nhất 6/12 cửa sổ hợp lệ
INPUT_HOURS = 24
FETCH_HOURS = 48                        # 24 giờ đầu vào + ngữ cảnh bên trái để nội suy
MAX_DATA_AGE_S = 3 * HOUR_S             # dữ liệu cũ hơn thì không dự báo

SERIES_COLUMNS = ("pm25", "temp", "hum")
FEATURES = ("pm25", "temp", "hum", "hour_sin", "hour_cos")
TARGET_KINDS = ("pm25", "diff24")       # đích mô hình: mức PM2.5 hoặc sai phân lag 24
SCALER_FORMAT = 1

Series = list[tuple[int, float | None]]

# Tên hiển thị của mô hình (bot /forecast và báo cáo sáng)
MODEL_NAMES = {
    "persistence": "giữ nguyên giờ gần nhất",
    "seasonal_naive": "cùng giờ hôm qua",
    "lstm": "mạng LSTM",
}


# ---------------- dữ liệu vào ----------------
def hourly_series(conn, end_ts: int, hours: int = 48, column: str = "pm25") -> Series:
    """Trung bình theo giờ của cột `column` (pm25, temp, hum), `hours` giờ đủ kết thúc
    trước hoặc đúng end_ts.

    Trả về [(đầu giờ, giá trị hoặc None)] tăng dần; phần tử cuối là giờ đủ cuối cùng.
    Chỉ dùng dòng readings valid=1 có giá trị ở cột đó; giờ có ít hơn MIN_WINDOWS dòng
    thì None."""
    if column not in SERIES_COLUMNS:
        raise ValueError(f"cột không hỗ trợ: {column!r}")
    last = int(end_ts) // HOUR_S * HOUR_S - HOUR_S
    first = last - (hours - 1) * HOUR_S
    rows = conn.execute(
        f"SELECT (ts / ?) * ?, AVG({column}), COUNT(*) FROM readings "
        f"WHERE valid = 1 AND {column} IS NOT NULL AND ts >= ? AND ts < ? "
        "GROUP BY ts / ?",
        (HOUR_S, HOUR_S, first, last + HOUR_S, HOUR_S),
    ).fetchall()
    means = {int(h): float(avg) for h, avg, n in rows if n >= MIN_WINDOWS}
    return [(t, means.get(t)) for t in range(first, last + HOUR_S, HOUR_S)]


def prepare_input(series: Series, hours: int = INPUT_HOURS, max_gap_h: int = 2,
                  min_valid: int = 20):
    """numpy.ndarray `hours` giá trị (giờ đủ cuối cùng ở cuối) hoặc None.

    Khoảng trống tối đa max_gap_h giờ liên tiếp được nội suy tuyến tính. Khoảng trống ở đầu
    cửa sổ được nội suy từ điểm hợp lệ bên trái nếu có trong max_gap_h giờ ngay trước cửa sổ
    (series dài hơn `hours`), ngược lại lấy giá trị hợp lệ đầu tiên.
    None khi: chưa đủ `hours` điểm, có khoảng trống dài hơn (tính cả phần ngữ cảnh bên trái,
    nên khoảng trống vắt qua đầu cửa sổ cũng bị giới hạn max_gap_h), ít hơn min_valid điểm
    hợp lệ, hoặc điểm cuối bị thiếu (không có điểm bên phải để nội suy)."""
    if len(series) < hours:
        return None
    values = [v for _, v in series[-hours:]]
    if values[-1] is None or sum(v is not None for v in values) < min_valid:
        return None
    ext = [v for _, v in series[-(hours + max_gap_h):]]     # thêm ngữ cảnh bên trái
    run = 0
    for v in ext:                   # cùng đoạn sẽ được nội suy bên dưới
        run = run + 1 if v is None else 0
        if run > max_gap_h:
            return None

    import numpy as np
    x = np.array([math.nan if v is None else float(v) for v in ext], dtype=float)
    known = ~np.isnan(x)
    idx = np.arange(len(x))
    # np.interp giữ giá trị biên cho phần đầu thiếu (không có điểm bên trái)
    x[~known] = np.interp(idx[~known], idx[known], x[known])
    return x[-hours:]


def build_features(conn, now: int, pm25_series: Series, x_pm25) -> dict:
    """dict đầu vào cho predictor (xem docstring module). x_pm25 = prepare_input(pm25_series)."""
    import numpy as np
    feats = {"pm25": x_pm25}
    for col in ("temp", "hum"):
        v = prepare_input(hourly_series(conn, now, hours=FETCH_HOURS, column=col))
        if v is not None:
            feats[col] = v
    hours = np.array([time.localtime(ts).tm_hour for ts, _ in pm25_series[-INPUT_HOURS:]],
                     dtype=float)
    feats["hour_sin"] = np.sin(2 * np.pi * hours / 24)
    feats["hour_cos"] = np.cos(2 * np.pi * hours / 24)
    return feats


# ---------------- mô hình đối chứng ----------------
def _last_valid(series: Series):
    for ts, v in reversed(series):
        if v is not None:
            return ts, v
    return None


def persistence(series: Series) -> list[float]:
    """Giá trị giờ hợp lệ gần nhất lặp lại 6 lần. Chuỗi không có giá trị nào: ValueError."""
    found = _last_valid(series)
    if found is None:
        raise ValueError("chuỗi PM2.5 không có giờ hợp lệ")
    return [float(found[1])] * HORIZONS


def seasonal_naive(x) -> list[float]:
    """Cùng giờ hôm qua. x là đầu vào 24 giờ từ prepare_input (x[-1] là giờ đủ cuối L).

    Mục tiêu horizon k là giờ L + k; 24 giờ trước đó là L + k - 24, tức x[k - 1]."""
    if len(x) < INPUT_HOURS:
        raise ValueError(f"cần {INPUT_HOURS} giờ đầu vào, có {len(x)}")
    start = len(x) - INPUT_HOURS
    return [float(v) for v in x[start:start + HORIZONS]]


def _check_prediction(preds) -> list[float]:
    """Kết quả predictor phải là 6 số hữu hạn; sai thì ValueError."""
    try:
        out = [float(v) for v in preds]
    except TypeError:
        raise ValueError(f"kết quả không phải dãy số: {preds!r}") from None
    if len(out) != HORIZONS:
        raise ValueError(f"cần {HORIZONS} giá trị, có {len(out)}")
    if not all(math.isfinite(v) for v in out):
        raise ValueError(f"có giá trị không hữu hạn: {out}")
    return out


# ---------------- mô hình TFLite ----------------
def _finite_list(value, n: int, what: str):
    import numpy as np
    if not isinstance(value, list) or len(value) != n:
        raise ValueError(f"{what} cần danh sách {n} số")
    arr = np.array([float(v) for v in value], dtype=float)
    if not np.isfinite(arr).all():
        raise ValueError(f"{what} có giá trị không hữu hạn")
    return arr


def read_scaler_spec(path) -> dict:
    """Đọc và kiểm tra scaler.json; sai định dạng thì ValueError (thiếu file: OSError).

    Khoá: format (=1), model_version, features (thứ tự cột đầu vào, thuộc FEATURES, có
    pm25), data_min/data_max (tham số MinMaxScaler fit trên tập train, theo thứ tự features),
    target {kind: 'pm25' | 'diff24', min, max} (MinMaxScaler của đích), n_in=24, n_out=6.
    Với 'diff24', đầu ra mô hình là PM2.5[L+k] - PM2.5[L+k-24] và được cộng lại với đầu vào.
    Kết quả là dict, data_min/data_max là numpy.ndarray."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("scaler.json phải là một object")
    if raw.get("format") != SCALER_FORMAT:
        raise ValueError(f"format phải là {SCALER_FORMAT}")
    feats = raw.get("features")
    if (not isinstance(feats, list) or not feats or len(set(feats)) != len(feats)
            or any(f not in FEATURES for f in feats) or "pm25" not in feats):
        raise ValueError(f"features không hợp lệ: {feats!r}")
    if raw.get("n_in") != INPUT_HOURS or raw.get("n_out") != HORIZONS:
        raise ValueError(f"cần n_in={INPUT_HOURS}, n_out={HORIZONS}")
    dmin = _finite_list(raw.get("data_min"), len(feats), "data_min")
    dmax = _finite_list(raw.get("data_max"), len(feats), "data_max")
    if (dmax < dmin).any():
        raise ValueError("data_max nhỏ hơn data_min")
    target = raw.get("target")
    if not isinstance(target, dict) or target.get("kind") not in TARGET_KINDS:
        raise ValueError(f"target không hợp lệ: {target!r}")
    tmin, tmax = _finite_list([target.get("min"), target.get("max")], 2, "target min/max")
    if tmax < tmin:
        raise ValueError("target.max nhỏ hơn target.min")
    return {"model_version": str(raw.get("model_version", "")), "features": list(feats),
            "data_min": dmin, "data_max": dmax, "target_kind": target["kind"],
            "target_min": float(tmin), "target_max": float(tmax),
            "n_in": INPUT_HOURS, "n_out": HORIZONS}


def _range(lo, hi):
    """max - min; khoảng bằng 0 thay bằng 1 như MinMaxScaler của scikit-learn."""
    import numpy as np
    r = np.asarray(hi, dtype=float) - np.asarray(lo, dtype=float)
    return np.where(r == 0, 1.0, r)


def scale_features(spec: dict, x):
    """Chuẩn hoá ma trận (giờ, đặc trưng) về [0, 1] như MinMaxScaler.transform."""
    import numpy as np
    return ((np.asarray(x, dtype=float) - spec["data_min"])
            / _range(spec["data_min"], spec["data_max"]))


def unscale_target(spec: dict, y):
    """Đảo chuẩn hoá đầu ra mô hình về đơn vị gốc (như MinMaxScaler.inverse_transform)."""
    import numpy as np
    return (np.asarray(y, dtype=float) * float(_range(spec["target_min"], spec["target_max"]))
            + spec["target_min"])


def _interpreter_class():
    """Lớp Interpreter: ai_edge_litert (Pi), rồi tflite_runtime, rồi tensorflow.lite.

    Không có thư viện nào thì ImportError."""
    try:
        from ai_edge_litert.interpreter import Interpreter
        return Interpreter
    except ImportError:
        pass
    try:
        from tflite_runtime.interpreter import Interpreter
        return Interpreter
    except ImportError:
        pass
    import tensorflow as tf
    return tf.lite.Interpreter


def load_predictor(model_path, scaler_path) -> Callable[[dict], list[float]] | None:
    """Nạp mô hình .tflite và scaler.json; trả về predictor cho run_forecast hoặc None.

    None (kèm log) khi thiếu file, scaler.json sai, không có thư viện LiteRT, file .tflite
    hỏng hoặc kích thước vào/ra của mô hình không khớp scaler.json. Predictor raise khi
    thiếu đặc trưng, mảng không đúng 24 giờ hoặc có NaN, để run_forecast về đối chứng."""
    import numpy as np
    model_path, scaler_path = Path(model_path), Path(scaler_path)
    if not model_path.is_file() or not scaler_path.is_file():
        log.info("Chưa có mô hình học máy (%s, %s): dùng đối chứng", model_path, scaler_path)
        return None
    try:
        spec = read_scaler_spec(scaler_path)
    except (OSError, ValueError, TypeError) as e:
        log.warning("scaler.json không hợp lệ, dùng đối chứng: %s", e)
        return None
    try:
        interpreter_cls = _interpreter_class()
    except ImportError as e:
        log.warning("Không có thư viện LiteRT (ai-edge-litert), dùng đối chứng: %s", e)
        return None
    n_in, n_out, n_feat = spec["n_in"], spec["n_out"], len(spec["features"])
    try:
        interp = interpreter_cls(model_path=str(model_path))
        interp.allocate_tensors()
        inp = interp.get_input_details()
        out = interp.get_output_details()
        if len(inp) != 1 or len(out) != 1:
            raise ValueError(f"mô hình cần 1 đầu vào, 1 đầu ra; có {len(inp)} và {len(out)}")
        in_shape = tuple(int(v) for v in inp[0]["shape"])
        out_shape = tuple(int(v) for v in out[0]["shape"])
        if in_shape != (1, n_in, n_feat) or out_shape != (1, n_out):
            raise ValueError(f"mô hình nhận {in_shape} trả {out_shape}, scaler.json khai "
                             f"{(1, n_in, n_feat)} -> {(1, n_out)}")
        if np.dtype(inp[0]["dtype"]) != np.float32:
            raise ValueError(f"đầu vào mô hình phải là float32, có {inp[0]['dtype']}")
    except Exception as e:
        log.warning("Không nạp được mô hình %s, dùng đối chứng: %s", model_path, e)
        return None
    in_idx, out_idx = inp[0]["index"], out[0]["index"]
    pm25_col = spec["features"].index("pm25")

    def predict(features: dict) -> list[float]:
        cols = []
        for name in spec["features"]:
            arr = np.asarray(features[name], dtype=float)       # thiếu đặc trưng: KeyError
            if arr.shape != (n_in,):
                raise ValueError(f"đặc trưng {name} cần {n_in} giá trị, có {arr.shape}")
            cols.append(arr)
        x = np.column_stack(cols)
        if not np.isfinite(x).all():
            raise ValueError("đầu vào có NaN hoặc vô cực")
        interp.set_tensor(in_idx, scale_features(spec, x).astype(np.float32)[None])
        interp.invoke()
        y = np.asarray(interp.get_tensor(out_idx), dtype=float).reshape(-1)
        if y.size != n_out:
            raise ValueError(f"mô hình trả {y.size} giá trị, cần {n_out}")
        y = unscale_target(spec, y)
        if spec["target_kind"] == "diff24":
            # đích horizon k là giờ L + k; cùng giờ hôm qua (L + k - 24) là x[k - 1]
            y = y + x[:n_out, pm25_col]
        return [float(v) for v in y]

    predict.spec = spec
    log.info("Đã nạp mô hình học máy %s (phiên bản %s, đích %s, đặc trưng %s)",
             model_path.name, spec["model_version"], spec["target_kind"],
             ",".join(spec["features"]))
    return predict


# ---------------- chạy dự báo ----------------
def run_forecast(conn, now: int,
                 predictor: Callable[[dict], list[float]] | None = None
                 ) -> tuple[str, list[float]] | None:
    """Dự báo 6 giờ tới lúc now, ghi vào bảng forecasts. Trả về (model, 6 giá trị),
    hoặc None nếu không đủ dữ liệu (không ghi gì). Lỗi ghi DB thì rollback và raise."""
    now = int(now)
    series = hourly_series(conn, now, hours=FETCH_HOURS)
    found = _last_valid(series)
    if found is None:
        log.info("Chưa có giờ PM2.5 hợp lệ nào trong %d giờ qua: không dự báo", FETCH_HOURS)
        return None
    if now - (found[0] + HOUR_S) > MAX_DATA_AGE_S:
        log.info("Giờ PM2.5 hợp lệ mới nhất kết thúc quá 3 giờ trước: không dự báo")
        return None

    x = prepare_input(series)
    model, preds = None, None
    if predictor is not None:
        if x is None:
            log.warning("Không đủ 24 giờ đầu vào cho mô hình học máy, dùng đối chứng")
        else:
            try:
                feats = build_features(conn, now, series, x)
                t0 = time.perf_counter()
                preds = _check_prediction(predictor(feats))
                log.info("Mô hình học máy suy luận trong %.1f ms",
                         (time.perf_counter() - t0) * 1000)
                model = "lstm"
            except Exception as e:
                log.warning("Mô hình học máy lỗi, dùng đối chứng: %s", e)
                preds = None
    if preds is None:
        if x is not None:
            model, preds = "seasonal_naive", seasonal_naive(x)
        else:
            model, preds = "persistence", persistence(series)
    preds = [max(0.0, float(v)) for v in preds]

    made_at = now // HOUR_S * HOUR_S
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO forecasts (made_at, horizon_h, pm25_pred, model) "
            "VALUES (?, ?, ?, ?)",
            [(made_at, k + 1, v, model) for k, v in enumerate(preds)],
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    log.info("Dự báo made_at=%s model=%s: %s", made_at, model,
             ", ".join(f"{v:.1f}" for v in preds))
    return model, preds


# ---------------- đọc dự báo ----------------
def latest_rows(conn, now: int, max_age_s: int):
    """(made_at, model, [(horizon, giá trị)]) của dự báo mới nhất không ở tương lai,
    hoặc None nếu chưa có hay đã cũ hơn max_age_s."""
    now = int(now)
    row = conn.execute("SELECT MAX(made_at) FROM forecasts WHERE made_at <= ?", (now,)).fetchone()
    made_at = row[0] if row else None
    if made_at is None or now - made_at > max_age_s:
        return None
    rows = conn.execute(
        "SELECT horizon_h, pm25_pred, model FROM forecasts WHERE made_at = ? ORDER BY horizon_h",
        (made_at,),
    ).fetchall()
    model = rows[0][2] if rows else ""
    return int(made_at), model, [(int(h), float(v)) for h, v, _ in rows]


def latest_forecast(conn, now: int, max_age_s: int) -> tuple[float, int] | None:
    """(TB dự báo horizon 1–3, made_at) của dự báo mới nhất, hoặc None nếu chưa có,
    đã cũ hơn max_age_s, hoặc thiếu một trong ba horizon đầu."""
    found = latest_rows(conn, now, max_age_s)
    if found is None:
        return None
    made_at, _, rows = found
    first3 = {h: v for h, v in rows if h <= 3}
    if sorted(first3) != [1, 2, 3]:
        return None
    return sum(first3.values()) / 3, made_at


# ---------------- chạy một lần ----------------
def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from aiot.config import load_config
    from aiot.db import connect, init_schema

    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Dự báo PM2.5 6 giờ tới (chạy một lần)")
    parser.add_argument("--config", default=str(root / "config.yaml"))
    args = parser.parse_args(argv)

    cfg = load_config(args.config, root / ".env")
    if not is_clock_synced():
        # Pi không có RTC: trước khi có NTP, giờ có thể là lúc tắt máy lần trước, dự báo ghi
        # với made_at đó sẽ bị controller và /forecast coi là còn mới. Timer chạy lại giờ sau.
        log.warning("Đồng hồ Pi chưa đồng bộ NTP: bỏ lần dự báo này")
        return 0
    conn = connect(cfg.db_path)
    try:
        init_schema(conn)
        # Nạp lại mỗi lần chạy: xoá file mô hình thì lần sau tự về đối chứng
        predictor = load_predictor(cfg.forecast.model_path, cfg.forecast.scaler_path)
        run_forecast(conn, int(time.time()), predictor=predictor)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
