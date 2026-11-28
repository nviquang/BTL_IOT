"""Đặc trưng cửa sổ (aiot/source_window.py) và mô hình logistic regression (aiot/source_model.py)."""
import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from aiot.source_model import (LABELS, LogRegModel, load_model, save_model,
                                spec_from_sklearn)
from aiot.source_window import BASE_S, WINDOW_FEATURES, compute_window_features


def flat_buffer(pm25=20.0, hum=70.0, temp=26.0, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    p = pm25 * np.exp(rng.normal(0.0, noise, BASE_S)) if noise else np.full(BASE_S, pm25)
    return {"pm1": p * 0.65, "pm25": p, "pm10": p * 1.2, "temp": np.full(BASE_S, temp),
            "hum": np.full(BASE_S, hum), "lux": np.full(BASE_S, 220.0)}


def feats(buf):
    return dict(zip(WINDOW_FEATURES, compute_window_features(buf)))


# ---------------- đặc trưng ----------------
def test_window_features_shape_and_none():
    buf = flat_buffer()
    buf["pm25"][-60:] = np.nan
    assert compute_window_features(buf) is None
    assert compute_window_features(flat_buffer()).shape == (len(WINDOW_FEATURES),) == (77,)


@pytest.mark.parametrize("level", [5.0, 40.0, 170.0, 250.0])
def test_flat_background_any_level_looks_calm(level):
    """Để yên ở mọi mức nền: độ lệch so với nền gần 0, không có cú nhảy (ở nền rất thấp, phần
    +5 µg/m³ còn làm nhiễu nhỏ hơn nữa)."""
    f = feats(flat_buffer(pm25=level, noise=0.005, seed=1))
    assert max(abs(v) for k, v in f.items() if k.startswith("d25")) < 0.05
    assert all(v == 0.0 for k, v in f.items() if k.startswith(("ch", "ct")))
    assert f["max_jump_z"] < 1.0 and f["miss_frac"] == 0.0


def test_window_spike_shows_in_last_blocks():
    buf = flat_buffer(pm25=170.0)
    buf["pm25"][-20:] = 1000.0
    f = feats(buf)
    assert f["d25_23"] > 1.5 and abs(f["d25_00"]) < 1e-6
    assert f["d25_max"] > 1.5 and f["max_jump_z"] > 10
    assert f["ch_max"] == 0.0 and f["ct_max"] == 0.0


def test_humidity_and_temp_alone_give_no_interaction():
    """Ẩm/nhiệt tăng mà bụi không đổi (thở gần, phòng tắm): tích bụi × ẩm/nhiệt bằng 0."""
    buf = flat_buffer(hum=65.0, temp=26.0)
    buf["hum"][-60:] = 78.0
    buf["temp"][-60:] = 28.0
    f = feats(buf)
    assert f["ch_max"] == 0.0 and f["ct_max"] == 0.0 and abs(f["d25_max"]) < 1e-6


def test_dust_with_humidity_rise_gives_interaction():
    """Hơi nước: bụi ×2 cùng ẩm +13 %, nhiệt +2 °C trong 60 s cuối."""
    buf = flat_buffer(pm25=40.0, hum=65.0, temp=26.0)
    buf["pm25"][-60:] = 80.0
    buf["hum"][-60:] = 78.0
    buf["temp"][-60:] = 28.0
    f = feats(buf)
    dust = np.log(85.0) - np.log(45.0) - 0.1
    assert f["ch_23"] == pytest.approx(dust * 13.0, rel=0.05)
    assert f["ct_23"] == pytest.approx(dust * 2.0, rel=0.05)
    assert f["ch_00"] == 0.0


def test_window_features_gaps_are_counted():
    buf = flat_buffer(pm25=20.0)
    buf["pm25"][-100:-40] = np.nan
    f = compute_window_features(buf)
    assert np.isfinite(f).all()
    assert dict(zip(WINDOW_FEATURES, f))["miss_frac"] == pytest.approx(12 / 24, abs=0.05)


def test_missing_block_during_smoke_is_not_back_to_baseline():
    """Khói đọng (PM ×1,4 so với nền) và khối 5 giây mới nhất chưa có mẫu: khối đó giữ mức của
    khối trước, không bị coi là đã về nền (lỗi 28/11 23:34: nhãn nhảy theo nhịp ghi DB)."""
    buf = flat_buffer(pm25=85.0)
    buf["pm25"][-300:] = 120.0
    full = feats(buf)
    buf["pm25"][-5:] = np.nan
    gap = feats(buf)
    assert gap["d25_23"] == pytest.approx(full["d25_23"], abs=1e-9) and gap["d25_23"] > 0.3
    assert gap["d25_max"] == pytest.approx(full["d25_max"], abs=1e-9)


def test_window_features_short_history():
    """Mới bật trạm: chỉ có 90 giây dữ liệu, phần còn lại là NaN."""
    buf = flat_buffer(pm25=30.0)
    for c in buf:
        buf[c][:-90] = np.nan
    f = compute_window_features(buf)
    assert np.isfinite(f).all() and abs(feats(buf)["d25_23"]) < 1e-6


# ---------------- mô hình ----------------
def fitted(seed=0):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(300, len(WINDOW_FEATURES)))
    y = rng.integers(0, 3, 300)
    sc = StandardScaler().fit(X)
    lr = LogisticRegression(max_iter=500).fit(sc.transform(X), y)
    return X, sc, lr, spec_from_sklearn(sc, lr, [LABELS[c] for c in lr.classes_])


def test_logreg_json_matches_sklearn(tmp_path):
    X, sc, lr, spec = fitted()
    save_model(LogRegModel(spec), tmp_path / "m.json")
    m = load_model(tmp_path / "m.json")
    np.testing.assert_allclose(m.predict_proba_batch(X[:50]), lr.predict_proba(sc.transform(X[:50])),
                               atol=1e-9)
    np.testing.assert_allclose(m.predict_proba(X[0]), m.predict_proba_batch(X[:1])[0])


def test_logreg_rejects_wrong_features():
    spec = fitted()[3]
    spec["features"] = spec["features"][:-1]
    with pytest.raises(ValueError):
        LogRegModel(spec)


def test_logreg_rejects_old_labels():
    spec = fitted()[3]
    spec["classes"] = ["normal", "cooking", "smoking"]
    with pytest.raises(ValueError):
        LogRegModel(spec)


def test_logreg_extreme_input_finite():
    p = LogRegModel(fitted()[3]).predict_proba(np.full(len(WINDOW_FEATURES), 50.0))
    assert np.isfinite(p).all() and p.sum() == pytest.approx(1.0)


def test_anomaly_score_is_one_minus_normal():
    m = LogRegModel(fitted()[3])
    p = m.predict_proba(np.zeros(len(WINDOW_FEATURES)))
    assert m.anomaly_score(p) == pytest.approx(1 - p[m.classes.index("normal")])
