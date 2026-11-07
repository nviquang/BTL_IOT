"""Test nạp mô hình TFLite trên Pi: scaler.json, load_predictor và rơi về đối chứng (Task 12).

Phần lớn test dùng interpreter giả nên không cần TensorFlow/LiteRT; test cuối dùng
TensorFlow thật (bỏ qua nếu chưa cài training/requirements.txt)."""
import json
import math
import time

import numpy as np
import pytest

from aiot import forecast
from aiot.db import connect, init_schema
from aiot.forecast import load_predictor, run_forecast

H = 3600
BASE = 1_793_000_000 // H * H
NOW = BASE + 100 * H + 5 * 60
LAST = BASE + 99 * H

FEATURES = ["pm25", "temp", "hum", "hour_sin", "hour_cos"]


def make_spec(features=FEATURES, target=None, n_in=24, n_out=6):
    """scaler.json hợp lệ: pm25 trong [0, 200], temp [10, 40], hum [20, 100], sin/cos [-1, 1]."""
    lo = {"pm25": 0.0, "temp": 10.0, "hum": 20.0, "hour_sin": -1.0, "hour_cos": -1.0}
    hi = {"pm25": 200.0, "temp": 40.0, "hum": 100.0, "hour_sin": 1.0, "hour_cos": 1.0}
    return {
        "format": 1,
        "model_version": "test",
        "features": list(features),
        "data_min": [lo[f] for f in features],
        "data_max": [hi[f] for f in features],
        "target": target or {"kind": "pm25", "min": 0.0, "max": 200.0},
        "n_in": n_in,
        "n_out": n_out,
    }


class FakeInterpreter:
    """Thay Interpreter của LiteRT. File mô hình giả có dạng b"FAKE n_in n_feat n_out";
    nội dung khác thì báo lỗi như file .tflite hỏng. Đầu ra: giá trị chuẩn hoá của PM2.5
    giờ cuối (cột 0) lặp lại, hoặc hằng số `const` nếu có."""
    const = None

    def __init__(self, model_path):
        data = open(model_path, "rb").read()
        if not data.startswith(b"FAKE"):
            raise ValueError("Model provided has model identifier 'xxxx', should be 'TFL3'")
        self.n_in, self.n_feat, self.n_out = (int(v) for v in data.split()[1:4])
        self.x = None
        self.out = None

    def allocate_tensors(self):
        pass

    def get_input_details(self):
        return [{"index": 0, "shape": np.array([1, self.n_in, self.n_feat]), "dtype": np.float32}]

    def get_output_details(self):
        return [{"index": 1, "shape": np.array([1, self.n_out]), "dtype": np.float32}]

    def set_tensor(self, index, value):
        assert index == 0
        if tuple(value.shape) != (1, self.n_in, self.n_feat) or value.dtype != np.float32:
            raise ValueError(f"sai kích thước đầu vào {value.shape} {value.dtype}")
        self.x = value

    def invoke(self):
        v = self.const if self.const is not None else self.x[0, -1, 0]
        self.out = np.full((1, self.n_out), v, dtype=np.float32)

    def get_tensor(self, index):
        assert index == 1
        return self.out


@pytest.fixture
def fake_litert(monkeypatch):
    monkeypatch.setattr(forecast, "_interpreter_class", lambda: FakeInterpreter)
    FakeInterpreter.const = None
    yield FakeInterpreter
    FakeInterpreter.const = None


def write_model(tmp_path, spec, shape=None, name="m.tflite"):
    n_in, n_feat, n_out = shape or (spec["n_in"], len(spec["features"]), spec["n_out"])
    model = tmp_path / name
    model.write_bytes(f"FAKE {n_in} {n_feat} {n_out}".encode())
    scaler = tmp_path / "scaler.json"
    scaler.write_text(json.dumps(spec), encoding="utf-8")
    return model, scaler


def inputs(pm25_last=50.0):
    hours = np.arange(24)
    return {
        "pm25": np.linspace(10.0, pm25_last, 24),
        "temp": np.full(24, 25.0),
        "hum": np.full(24, 80.0),
        "hour_sin": np.sin(2 * np.pi * hours / 24),
        "hour_cos": np.cos(2 * np.pi * hours / 24),
    }


# ---------------- nạp mô hình ----------------
def test_load_predictor_missing_file_returns_none(tmp_path, fake_litert):
    model, scaler = write_model(tmp_path, make_spec())
    assert load_predictor(tmp_path / "khong_co.tflite", scaler) is None
    assert load_predictor(model, tmp_path / "khong_co.json") is None
    assert load_predictor(model, scaler) is not None


def test_load_predictor_corrupt_file_returns_none(tmp_path, fake_litert):
    model, scaler = write_model(tmp_path, make_spec())
    model.write_bytes(b"\x00\x01 day khong phai tflite")
    assert load_predictor(model, scaler) is None

    model, scaler = write_model(tmp_path, make_spec())
    scaler.write_text("{ khong phai json", encoding="utf-8")
    assert load_predictor(model, scaler) is None


@pytest.mark.parametrize("change", [
    {"n_in": 12},                                   # khác 24 giờ của run_forecast
    {"n_out": 3},                                   # khác 6 horizon
    {"data_min": [0.0]},                            # số min/max không khớp số đặc trưng
    {"features": ["pm25", "gio"]},                  # đặc trưng run_forecast không có
    {"target": {"kind": "log", "min": 0.0, "max": 1.0}},
    {"target": {"kind": "pm25"}},                   # thiếu min/max của đích
    {"data_max": [200.0, 40.0, 100.0, 1.0, math.nan]},
    {"features": None},
])
def test_load_predictor_invalid_scaler_returns_none(tmp_path, fake_litert, change):
    spec = make_spec()
    spec.update(change)
    model, scaler = write_model(tmp_path, make_spec())
    scaler.write_text(json.dumps(spec), encoding="utf-8")
    assert load_predictor(model, scaler) is None


def test_load_predictor_without_runtime_returns_none(tmp_path, monkeypatch):
    def missing():
        raise ImportError("No module named 'ai_edge_litert'")

    monkeypatch.setattr(forecast, "_interpreter_class", missing)
    model, scaler = write_model(tmp_path, make_spec())
    assert load_predictor(model, scaler) is None


def test_load_predictor_model_shape_mismatch_returns_none(tmp_path, fake_litert):
    spec = make_spec()
    for shape in [(24, 3, 6), (12, 5, 6), (24, 5, 1)]:
        model, scaler = write_model(tmp_path, spec, shape=shape)
        assert load_predictor(model, scaler) is None


# ---------------- suy luận ----------------
def test_predictor_level_target_inverts_scaling(tmp_path, fake_litert):
    model, scaler = write_model(tmp_path, make_spec())
    predict = load_predictor(model, scaler)
    # mô hình giả trả PM2.5 chuẩn hoá của giờ cuối -> đảo ngược ra đúng giá trị đó
    assert predict(inputs(pm25_last=50.0)) == pytest.approx([50.0] * 6, abs=1e-4)


def test_predictor_diff24_target_adds_same_hour_yesterday(tmp_path, fake_litert):
    spec = make_spec(target={"kind": "diff24", "min": -20.0, "max": 20.0})
    model, scaler = write_model(tmp_path, spec)
    predict = load_predictor(model, scaler)
    x = inputs()
    fake_litert.const = 0.75                       # sai phân chuẩn hoá 0,75 -> +10 µg/m³
    # đích horizon k là giờ L + k, cùng giờ hôm qua là x[k - 1]
    assert predict(x) == pytest.approx(list(x["pm25"][:6] + 10.0), abs=1e-4)


def test_predictor_uses_only_declared_features(tmp_path, fake_litert):
    spec = make_spec(features=["pm25", "hour_sin", "hour_cos"])
    model, scaler = write_model(tmp_path, spec)
    predict = load_predictor(model, scaler)
    x = inputs(pm25_last=30.0)
    del x["temp"], x["hum"]
    assert predict(x) == pytest.approx([30.0] * 6, abs=1e-4)


def test_predictor_raises_on_missing_feature_or_wrong_length(tmp_path, fake_litert):
    model, scaler = write_model(tmp_path, make_spec())
    predict = load_predictor(model, scaler)
    x = inputs()
    del x["hum"]
    with pytest.raises(KeyError):
        predict(x)
    x = inputs()
    x["temp"] = x["temp"][:23]
    with pytest.raises(ValueError):
        predict(x)
    x = inputs()
    x["pm25"][3] = math.nan
    with pytest.raises(ValueError):
        predict(x)


# ---------------- run_forecast dùng predictor đã nạp ----------------
@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    yield c
    c.close()


def fill(conn, first, last, pm25, temp=None, hum=None):
    for t in range(first, last + 1, H):
        for i in range(12):
            conn.execute("INSERT INTO readings (ts, pm25, temp, hum, valid) VALUES (?, ?, ?, ?, 1)",
                         (t + i * 300, pm25, temp, hum))
    conn.commit()


def test_predictor_shape_mismatch_falls_back(tmp_path, fake_litert, conn):
    fill(conn, LAST - 23 * H, LAST, 20.0)          # chỉ có PM2.5, không có temp/hum
    model, scaler = write_model(tmp_path, make_spec())
    predict = load_predictor(model, scaler)
    assert predict is not None
    # scaler.json cần temp/hum nhưng DB không có: predictor lỗi -> cùng giờ hôm qua
    assert run_forecast(conn, NOW, predictor=predict) == ("seasonal_naive", [20.0] * 6)

    # scaler.json khai 5 đặc trưng nhưng mô hình nhận 3: không nạp, dùng đối chứng
    model, scaler = write_model(tmp_path, make_spec(), shape=(24, 3, 6), name="m3.tflite")
    predict = load_predictor(model, scaler)
    assert predict is None
    assert run_forecast(conn, NOW, predictor=predict)[0] == "seasonal_naive"


def test_run_forecast_with_loaded_predictor(tmp_path, fake_litert, conn):
    fill(conn, LAST - 47 * H, LAST, 40.0, temp=25.0, hum=80.0)
    model, scaler = write_model(tmp_path, make_spec())
    model_name, preds = run_forecast(conn, NOW, predictor=load_predictor(model, scaler))
    assert model_name == "lstm"
    assert preds == pytest.approx([40.0] * 6, abs=1e-4)


def test_main_uses_configured_model(tmp_path, fake_litert, monkeypatch):
    monkeypatch.setattr(forecast, "is_clock_synced", lambda: True)
    now = int(time.time())
    last = now // H * H - H
    db = tmp_path / "aiot.db"
    c = connect(db)
    init_schema(c)
    fill(c, last - 23 * H, last, 33.0, temp=25.0, hum=80.0)
    c.close()
    model, scaler = write_model(tmp_path, make_spec())
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"db_path: {db.as_posix()}\nforecast:\n  model_path: {model.as_posix()}\n"
                   f"  scaler_path: {scaler.as_posix()}\n", encoding="utf-8")
    def models():
        c = connect(db)
        got = c.execute("SELECT model, COUNT(*) FROM forecasts GROUP BY model").fetchall()
        c.close()
        return got

    assert forecast.main(["--config", str(cfg)]) == 0
    assert models() == [("lstm", 6)]
    model.unlink()                                 # xoá file mô hình: lần sau về đối chứng
    assert forecast.main(["--config", str(cfg)]) == 0
    assert models() == [("seasonal_naive", 6)]     # lần chạy sau ghi đè cùng made_at


# ---------------- scaler.json khớp MinMaxScaler ----------------
def test_scaler_json_roundtrip(tmp_path):
    pytest.importorskip("pandas")
    pytest.importorskip("sklearn")
    from sklearn.preprocessing import MinMaxScaler

    from training.train_lstm import scaler_spec

    rng = np.random.default_rng(0)
    train = rng.normal([50, 27, 75, 0, 0], [30, 5, 10, 0.7, 0.7], size=(500, 5))
    train[:, 2] = 80.0                             # cột hằng số: sklearn giữ scale = 1
    scaler = MinMaxScaler().fit(train)
    target = MinMaxScaler().fit(rng.normal(0, 15, size=(300, 1)))
    spec = scaler_spec(scaler, FEATURES, "diff24", target, model_version="t")
    path = tmp_path / "scaler.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    loaded = forecast.read_scaler_spec(path)

    x = rng.normal([50, 27, 75, 0, 0], [40, 8, 12, 0.7, 0.7], size=(24, 5))
    np.testing.assert_allclose(forecast.scale_features(loaded, x), scaler.transform(x),
                               atol=1e-6)
    y = rng.uniform(-0.2, 1.2, size=6)
    np.testing.assert_allclose(forecast.unscale_target(loaded, y),
                               target.inverse_transform(y.reshape(-1, 1)).ravel(), atol=1e-6)


# ---------------- TFLite thật ----------------
def test_real_tflite_export_and_load(tmp_path):
    tf = pytest.importorskip("tensorflow")
    from training.train_lstm import build_model, export_tflite

    tf.keras.utils.set_random_seed(1)
    keras_model = build_model(n_in=24, n_feat=3, units=8)
    model = tmp_path / "pm25_lstm.tflite"
    export_tflite(keras_model, model, n_in=24, n_feat=3)
    spec = make_spec(features=["pm25", "hour_sin", "hour_cos"])
    scaler = tmp_path / "scaler.json"
    scaler.write_text(json.dumps(spec), encoding="utf-8")

    predict = load_predictor(model, scaler)
    assert predict is not None
    x = inputs()
    scaled = forecast.scale_features(forecast.read_scaler_spec(scaler),
                                     np.column_stack([x["pm25"], x["hour_sin"], x["hour_cos"]]))
    want = keras_model(scaled[None].astype(np.float32)).numpy()[0] * 200.0
    assert predict(x) == pytest.approx(list(want), abs=1e-3)

    model.write_bytes(b"TFL3 hong" * 10)           # file hỏng với interpreter thật
    assert load_predictor(model, scaler) is None
