"""Test chuẩn bị dữ liệu chuỗi thời gian cho huấn luyện (Task 11, chạy trên laptop)."""
import numpy as np
import pytest

# Phần huấn luyện cần pandas/scikit-learn (training/requirements.txt), Pi không cài
pd = pytest.importorskip("pandas")
pytest.importorskip("sklearn")

from aiot.db import connect, init_schema
from training.data import (add_time_features, chrono_split, difference, fit_scaler,
                           invert_difference, make_windows)
from training.export_hourly import export_hourly

FEATURES = ["pm25", "temp", "hum"]


def hourly_frame(n=100, start="2026-01-01 00:00"):
    idx = pd.date_range(start, periods=n, freq="h", tz="Asia/Ho_Chi_Minh")
    t = np.arange(n, dtype=float)
    return pd.DataFrame({
        "pm25": 30 + 10 * np.sin(2 * np.pi * t / 24) + t * 0.1,
        "temp": 25 + np.cos(2 * np.pi * t / 24),
        "hum": 70 + t % 5,
    }, index=idx)


# ---------------- chia tập và chuẩn hoá ----------------
def test_split_is_chronological_without_overlap():
    df = hourly_frame(200)
    train, val, test = chrono_split(df, train=0.7, val=0.15)
    assert train.index.max() < val.index.min()
    assert val.index.max() < test.index.min()
    assert len(train) + len(val) + len(test) == len(df)
    assert len(train) == 140 and len(val) == 30


def test_split_does_not_shuffle_unsorted_input():
    df = hourly_frame(50).iloc[::-1]
    train, val, test = chrono_split(df)
    assert train.index.is_monotonic_increasing
    assert train.index.max() < val.index.min() < test.index.min()


def test_scaler_fit_on_train_only():
    df = hourly_frame(100)
    train, val, test = chrono_split(df)
    val = val.copy()
    val.iloc[0, val.columns.get_loc("pm25")] = 10_000.0     # đỉnh lớn chỉ có ở val
    scaler = fit_scaler(train, FEATURES)
    assert scaler.data_max_[0] == pytest.approx(train["pm25"].max())
    assert scaler.data_max_[0] < 10_000.0
    assert list(scaler.feature_names_in_) == FEATURES


# ---------------- đặc trưng giờ ----------------
def test_add_time_features_sin_cos_hour():
    df = add_time_features(hourly_frame(24))
    assert df["hour_sin"].iloc[0] == pytest.approx(0.0)
    assert df["hour_cos"].iloc[0] == pytest.approx(1.0)
    assert df["hour_sin"].iloc[6] == pytest.approx(1.0)
    assert np.allclose(df["hour_sin"] ** 2 + df["hour_cos"] ** 2, 1.0)


# ---------------- cửa sổ trượt ----------------
def test_window_shapes():
    df = hourly_frame(100)
    X, y, target_times = make_windows(df[FEATURES], target="pm25", n_in=24, n_out=6)
    assert X.shape == (71, 24, 3)
    assert y.shape == (71, 6)
    assert len(target_times) == 71


def test_window_values_follow_feature_order_and_target():
    df = hourly_frame(40)
    cols = ["temp", "pm25"]
    X, y, tt = make_windows(df[cols], target="pm25", n_in=24, n_out=6)
    assert np.allclose(X[0], df[cols].iloc[0:24].to_numpy())
    assert np.allclose(y[0], df["pm25"].iloc[24:30].to_numpy())
    assert tt[0] == df.index[24]


def test_windows_skip_gaps():
    df = hourly_frame(100)
    missing = df.index[50]
    gappy = df.drop(index=missing)                        # thiếu hẳn một giờ trong index
    X, y, tt = make_windows(gappy[FEATURES], target="pm25", n_in=24, n_out=6)
    # cửa sổ bắt đầu ở giờ s chạm giờ 50 khi s <= 50 <= s + 29, tức s = 21..50 (30 cửa sổ)
    assert X.shape == (71 - 30, 24, 3)
    assert not np.isnan(X).any() and not np.isnan(y).any()
    for t in tt:
        first_input = t - pd.Timedelta(hours=24)
        last_target = t + pd.Timedelta(hours=5)
        assert not (first_input <= missing <= last_target)


def test_windows_skip_nan_hours():
    df = hourly_frame(100)
    df.iloc[50, df.columns.get_loc("hum")] = np.nan       # NaN ở đặc trưng đầu vào
    X, y, tt = make_windows(df[FEATURES], target="pm25", n_in=24, n_out=6)
    assert X.shape[0] == 71 - 30
    assert not np.isnan(X).any() and not np.isnan(y).any()


def test_target_never_inside_input():
    df = hourly_frame(100)
    X, y, tt = make_windows(df[FEATURES], target="pm25", n_in=24, n_out=6)
    for i, t in enumerate(tt):
        last_input = t - pd.Timedelta(hours=1)
        assert t > last_input
        pos = df.index.get_loc(t)
        assert np.allclose(X[i][-1], df[FEATURES].iloc[pos - 1].to_numpy())
        assert y[i][0] == pytest.approx(df["pm25"].iloc[pos])


def test_windows_use_scaler_on_features_and_target():
    df = hourly_frame(60)
    train, _, _ = chrono_split(df)
    scaler = fit_scaler(train, FEATURES)
    X, y, tt = make_windows(df[FEATURES], target="pm25", n_in=24, n_out=6, scaler=scaler)
    scaled = scaler.transform(df[FEATURES])
    assert np.allclose(X[0], scaled[0:24])
    assert np.allclose(y[0], scaled[24:30, 0])


# ---------------- sai phân ----------------
@pytest.mark.parametrize("lag", [1, 24])
def test_difference_roundtrip(lag):
    s = hourly_frame(100)["pm25"]
    d = difference(s, lag)
    assert len(d) == len(s) - lag
    assert not d.isna().any()
    history = s.iloc[:lag].to_numpy()                    # lag giá trị gốc ngay trước d
    restored = invert_difference(d.to_numpy(), history, lag)
    assert np.allclose(restored, s.iloc[lag:].to_numpy())


def test_invert_difference_forecast_from_tail():
    s = hourly_frame(60)["pm25"]
    d = difference(s, 24)
    # dự báo 6 bước sai phân tiếp theo sau phần đã biết s[:54]
    restored = invert_difference(d.iloc[30:36].to_numpy(), s.iloc[:54].to_numpy(), 24)
    assert np.allclose(restored, s.iloc[54:60].to_numpy())


# ---------------- xuất chuỗi giờ từ SQLite ----------------
H = 3600
BASE = 1_793_000_000 // H * H


def add_hour(conn, start, n, pm25, valid=1, offset=0):
    for i in range(n):
        conn.execute(
            "INSERT INTO readings (ts, pm1, pm25, pm10, temp, hum, lux, mq135_do, valid) "
            "VALUES (?, 1, ?, ?, 25, 70, 100, 1, ?)",
            (start + (offset + i) * 300, pm25, pm25 + 5, valid))
    conn.commit()


def test_export_excludes_invalid_rows(tmp_path):
    db = tmp_path / "aiot.db"
    conn = connect(db)
    init_schema(conn)
    add_hour(conn, BASE, 6, 20.0)                          # 6 hợp lệ ...
    add_hour(conn, BASE, 6, 999.0, valid=0, offset=6)      # ... 6 lỗi không được tính
    add_hour(conn, BASE + H, 5, 30.0)                      # chỉ 5 hợp lệ: bỏ giờ
    add_hour(conn, BASE + H, 7, 30.0, valid=0, offset=5)
    add_hour(conn, BASE + 2 * H, 12, 40.0)
    conn.close()

    out = tmp_path / "hourly.csv"
    n = export_hourly(db, out)
    df = pd.read_csv(out)
    assert n == 2
    assert list(df.columns) == ["timestamp", "pm25", "pm10", "temp", "hum"]
    assert df["pm25"].tolist() == [20.0, 40.0]
    assert df["pm10"].tolist() == [25.0, 45.0]
    ts = pd.to_datetime(df["timestamp"])
    assert (ts.dt.minute == 0).all()
    assert ts.iloc[0] == pd.Timestamp(BASE, unit="s", tz="UTC").tz_convert("Asia/Ho_Chi_Minh")
    assert str(ts.iloc[0].utcoffset()) == "7:00:00"


def test_export_cli(tmp_path):
    db = tmp_path / "aiot.db"
    conn = connect(db)
    init_schema(conn)
    add_hour(conn, BASE, 12, 15.0)
    conn.close()
    from training.export_hourly import main
    out = tmp_path / "hourly.csv"
    assert main(["--db", str(db), "--out", str(out)]) == 0
    assert len(pd.read_csv(out)) == 1
