"""Test dự báo đối chứng PM2.5: chuỗi giờ, nội suy, persistence, seasonal naive, ghi DB."""
import logging
import math
import time

import numpy as np
import pytest

from aiot import forecast
from aiot.db import connect, init_schema
from aiot.forecast import (hourly_series, latest_forecast, persistence, prepare_input,
                           run_forecast, seasonal_naive)

H = 3600
BASE = 1_793_000_000 // H * H        # đầu một giờ (epoch chia hết 3600)
NOW = BASE + 100 * H + 5 * 60        # 5 phút sau đầu giờ BASE+100h
LAST = BASE + 99 * H                 # giờ đủ cuối cùng trước NOW
MADE_AT = BASE + 100 * H


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    yield c
    c.close()


def add_hour(conn, start, value, n=12, valid=1):
    """n cửa sổ 5 phút đầu tiên của giờ start, cùng giá trị PM2.5."""
    for i in range(n):
        conn.execute("INSERT INTO readings (ts, pm25, valid) VALUES (?, ?, ?)",
                     (start + i * 300, value, valid))
    conn.commit()


def fill(conn, first, last, value_of):
    """Mỗi giờ từ first tới last (bao gồm) một giá trị value_of(giờ bắt đầu)."""
    for t in range(first, last + 1, H):
        add_hour(conn, t, value_of(t))


def rows(conn):
    return conn.execute(
        "SELECT made_at, horizon_h, pm25_pred, model FROM forecasts ORDER BY made_at, horizon_h"
    ).fetchall()


# ---------- chuỗi giờ ----------

def test_hourly_uses_only_valid_rows(conn):
    add_hour(conn, LAST, 10.0, n=6)                # 6 cửa sổ hợp lệ: đủ
    add_hour(conn, LAST + 1800, 1000.0, n=6, valid=0)   # nửa sau giờ: không hợp lệ, bị bỏ
    add_hour(conn, LAST - H, 20.0, n=5)            # chỉ 5/12 hợp lệ: thiếu
    add_hour(conn, LAST - 2 * H, 30.0)
    add_hour(conn, NOW - 300, 999.0, n=1)          # giờ hiện tại chưa đủ: không thuộc chuỗi
    series = hourly_series(conn, NOW, hours=4)
    assert series == [(LAST - 3 * H, None), (LAST - 2 * H, 30.0), (LAST - H, None), (LAST, 10.0)]
    assert all(ts % H == 0 for ts, _ in series)


def test_hourly_end_exactly_on_hour_boundary(conn):
    add_hour(conn, LAST, 7.0)
    series = hourly_series(conn, LAST + H, hours=1)
    assert series == [(LAST, 7.0)]


def test_hourly_mean_of_rows(conn):
    for i, v in enumerate([10.0, 20.0] * 6):
        conn.execute("INSERT INTO readings (ts, pm25, valid) VALUES (?, ?, 1)", (LAST + i * 300, v))
    conn.execute("INSERT INTO readings (ts, pm25, valid) VALUES (?, NULL, 1)", (LAST - H,))
    conn.commit()
    assert hourly_series(conn, NOW, hours=2) == [(LAST - H, None), (LAST, 15.0)]


# ---------- chuẩn bị đầu vào ----------

def series_of(values, last=LAST):
    n = len(values)
    return [(last - (n - 1 - i) * H, v) for i, v in enumerate(values)]


def test_small_gap_interpolated_large_gap_returns_none():
    values = [float(i) for i in range(24)]
    values[10] = values[11] = None                 # trống 2 giờ: nội suy tuyến tính
    x = prepare_input(series_of(values))
    assert isinstance(x, np.ndarray) and x.shape == (24,)
    assert x[10] == pytest.approx(10.0) and x[11] == pytest.approx(11.0)

    values[12] = None                              # trống 3 giờ liên tiếp
    assert prepare_input(series_of(values)) is None


def test_prepare_input_rejects_missing_last_point_and_too_few():
    values = [5.0] * 24
    values[-1] = None
    assert prepare_input(series_of(values)) is None
    assert prepare_input(series_of([5.0] * 23)) is None          # chưa đủ 24 giờ
    sparse = [5.0, None] * 12                                    # 12 điểm hợp lệ < 20
    assert prepare_input(series_of(sparse[1:] + [5.0])) is None


def test_prepare_input_uses_last_hours_only():
    values = [None] * 10 + [float(i) for i in range(24)]
    x = prepare_input(series_of(values))
    assert list(x) == [float(i) for i in range(24)]


# ---------- mô hình đối chứng ----------

def test_persistence_repeats_last_value():
    assert persistence(series_of([1.0, 2.0, 3.5, None])) == [3.5] * 6


def test_seasonal_naive_uses_same_hour_yesterday():
    x = np.arange(24, dtype=float)                 # x[i] là giờ LAST - (23 - i) giờ
    # Mục tiêu horizon k là giờ LAST + k; 24 giờ trước đó là LAST + k - 24, tức x[k - 1]
    assert seasonal_naive(x) == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]


# ---------- chạy dự báo ----------

def test_full_day_uses_seasonal_naive(conn):
    fill(conn, LAST - 23 * H, LAST, lambda t: float((t - BASE) // H))
    model, preds = run_forecast(conn, NOW)
    assert model == "seasonal_naive"
    first = float((LAST - 23 * H - BASE) // H)
    assert preds == [first + k for k in range(6)]
    assert rows(conn) == [(MADE_AT, k + 1, first + k, "seasonal_naive") for k in range(6)]


def test_cold_start_uses_persistence(conn):
    fill(conn, LAST - 9 * H, LAST - H, lambda t: 12.0)   # mới có 10 giờ dữ liệu
    add_hour(conn, LAST, 6.5)
    model, preds = run_forecast(conn, NOW)
    assert model == "persistence"
    assert preds == [6.5] * 6
    assert [r[3] for r in rows(conn)] == ["persistence"] * 6
    assert [r[0] for r in rows(conn)] == [MADE_AT] * 6


def test_no_forecast_from_old_data(conn, caplog):
    assert run_forecast(conn, NOW) is None         # chưa có dữ liệu
    assert rows(conn) == []

    add_hour(conn, LAST - 3 * H, 40.0)             # giờ hợp lệ mới nhất kết thúc 3 giờ 5 phút trước
    assert run_forecast(conn, NOW) is None
    assert rows(conn) == []

    add_hour(conn, LAST - 2 * H, 40.0)             # kết thúc 2 giờ 5 phút trước: vẫn dự báo
    model, preds = run_forecast(conn, NOW)
    assert model == "persistence" and preds == [40.0] * 6


def test_predictor_failure_falls_back(conn, caplog):
    fill(conn, LAST - 23 * H, LAST, lambda t: 20.0)

    def broken(x):
        raise RuntimeError("tflite lỗi")

    with caplog.at_level(logging.WARNING, logger="aiot.forecast"):
        model, preds = run_forecast(conn, NOW, predictor=broken)
    assert model == "seasonal_naive" and preds == [20.0] * 6
    assert any("tflite lỗi" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("bad", [[1.0] * 5, [1.0] * 5 + [math.nan], [1.0] * 5 + [math.inf], None])
def test_predictor_bad_output_falls_back(conn, bad):
    fill(conn, LAST - 23 * H, LAST, lambda t: 20.0)
    model, preds = run_forecast(conn, NOW, predictor=lambda x: bad)
    assert model == "seasonal_naive" and preds == [20.0] * 6


def test_predictor_used_when_ok(conn):
    fill(conn, LAST - 23 * H, LAST, lambda t: 20.0)
    seen = []

    def lstm(x):
        seen.append(x)
        return [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]

    model, preds = run_forecast(conn, NOW, predictor=lstm)
    assert model == "lstm" and preds == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    # chỉ có PM2.5 trong DB: dict không có temp/hum
    assert sorted(seen[0]) == ["hour_cos", "hour_sin", "pm25"]
    assert seen[0]["pm25"].shape == (24,)
    assert [r[3] for r in rows(conn)] == ["lstm"] * 6


def add_weather_hour(conn, start, pm25, temp, hum, n=12):
    for i in range(n):
        conn.execute("INSERT INTO readings (ts, pm25, temp, hum, valid) VALUES (?, ?, ?, ?, 1)",
                     (start + i * 300, pm25, temp, hum))
    conn.commit()


def test_predictor_gets_feature_dict(conn):
    for t in range(LAST - 23 * H, LAST + 1, H):
        i = (t - BASE) // H
        add_weather_hour(conn, t, float(i), 20.0 + i % 5, 60.0 + i % 7)
    seen = []
    run_forecast(conn, NOW, predictor=lambda x: seen.append(x) or [1.0] * 6)
    x = seen[0]
    assert sorted(x) == ["hour_cos", "hour_sin", "hum", "pm25", "temp"]
    hours = list(range(LAST - 23 * H, LAST + 1, H))
    idx = [(t - BASE) // H for t in hours]
    assert list(x["pm25"]) == [float(i) for i in idx]
    assert list(x["temp"]) == [20.0 + i % 5 for i in idx]
    assert list(x["hum"]) == [60.0 + i % 7 for i in idx]
    # giờ trong ngày (giờ địa phương) của từng giờ đầu vào, như add_time_features khi huấn luyện
    local_h = np.array([time.localtime(t).tm_hour for t in hours])
    np.testing.assert_allclose(x["hour_sin"], np.sin(2 * np.pi * local_h / 24))
    np.testing.assert_allclose(x["hour_cos"], np.cos(2 * np.pi * local_h / 24))
    assert all(v.shape == (24,) for v in x.values())


def test_weather_gap_too_long_omits_feature(conn):
    for t in range(LAST - 23 * H, LAST + 1, H):
        # temp/hum thiếu 3 giờ liên tiếp: không dựng được, chỉ truyền những gì có
        missing = LAST - 12 * H <= t <= LAST - 10 * H
        add_weather_hour(conn, t, 20.0, None if missing else 25.0, None if missing else 70.0)
    seen = []
    run_forecast(conn, NOW, predictor=lambda x: seen.append(x) or [1.0] * 6)
    assert sorted(seen[0]) == ["hour_cos", "hour_sin", "pm25"]


def test_leading_gap_interpolated_from_left_neighbour(conn):
    # 48 giờ dữ liệu tăng đều, thiếu 2 giờ đầu của cửa sổ 24 giờ
    first = LAST - 23 * H
    fill(conn, LAST - 47 * H, LAST, lambda t: float((t - BASE) // H))
    conn.execute("DELETE FROM readings WHERE ts >= ? AND ts < ?", (first, first + 2 * H))
    conn.commit()
    model, preds = run_forecast(conn, NOW)
    assert model == "seasonal_naive"
    # cùng giờ hôm qua đúng là target - 24 giờ (nội suy từ hàng xóm bên trái, không lấy biên)
    f = float((first - BASE) // H)
    assert preds == pytest.approx([f + k for k in range(6)])


def test_prepare_input_uses_left_context_for_leading_gap():
    values = [float(i) for i in range(30)]
    values[6] = values[7] = None                   # 2 giờ đầu của cửa sổ 24 giờ cuối
    x = prepare_input(series_of(values))
    assert x[0] == pytest.approx(6.0) and x[1] == pytest.approx(7.0)
    # không có hàng xóm bên trái thì vẫn lấy giá trị hợp lệ đầu tiên như trước
    x = prepare_input(series_of(values[6:]))
    assert x[0] == x[1] == 8.0


def test_prepare_input_rejects_gap_straddling_window_start():
    values = [float(i) for i in range(30)]
    values[5] = values[6] = values[7] = None       # 1 giờ trước cửa sổ + 2 giờ đầu cửa sổ
    assert prepare_input(series_of(values)) is None
    values[5] = 5.0                                # chỉ còn 2 giờ trống: vẫn nội suy được
    x = prepare_input(series_of(values))
    assert x[0] == pytest.approx(6.0) and x[1] == pytest.approx(7.0)


def test_negative_predictions_clipped_to_zero(conn):
    fill(conn, LAST - 23 * H, LAST, lambda t: 20.0)
    model, preds = run_forecast(conn, NOW, predictor=lambda x: [-3.0, 0.0, 5.0, -0.1, 1.0, 2.0])
    assert model == "lstm"
    assert preds == [0.0, 0.0, 5.0, 0.0, 1.0, 2.0]
    assert min(r[2] for r in rows(conn)) == 0.0


def test_rerun_same_hour_overwrites(conn):
    fill(conn, LAST - 2 * H, LAST - H, lambda t: 10.0)
    add_hour(conn, LAST, 10.0, n=6)
    run_forecast(conn, NOW)
    add_hour(conn, LAST + 1800, 30.0, n=6)         # thêm dữ liệu, chạy lại cùng giờ
    run_forecast(conn, NOW + 600)
    got = rows(conn)
    assert len(got) == 6
    assert all(r[0] == MADE_AT and r[2] == 20.0 for r in got)


def test_write_failure_writes_nothing(conn, monkeypatch):
    fill(conn, LAST - 2 * H, LAST, lambda t: 10.0)
    conn.execute("CREATE TRIGGER boom BEFORE INSERT ON forecasts WHEN NEW.horizon_h = 4 "
                 "BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    conn.commit()
    with pytest.raises(Exception):
        run_forecast(conn, NOW)
    assert rows(conn) == []


# ---------- đọc dự báo ----------

def put_forecast(conn, made_at, preds, model="persistence"):
    conn.executemany("INSERT INTO forecasts VALUES (?, ?, ?, ?)",
                     [(made_at, k + 1, v, model) for k, v in enumerate(preds)])
    conn.commit()


def test_latest_forecast_mean_of_first_three_horizons(conn):
    put_forecast(conn, MADE_AT - H, [500.0] * 6)                  # cũ hơn: bỏ qua
    put_forecast(conn, MADE_AT, [90.0, 100.0, 140.0, 999.0, 999.0, 999.0])
    assert latest_forecast(conn, NOW, 5400) == (pytest.approx(110.0), MADE_AT)


def test_stale_forecast_returns_none(conn):
    assert latest_forecast(conn, NOW, 5400) is None               # chưa có dự báo
    put_forecast(conn, MADE_AT, [50.0] * 6)
    assert latest_forecast(conn, MADE_AT + 5400, 5400) is not None
    assert latest_forecast(conn, MADE_AT + 5401, 5400) is None


def test_latest_forecast_incomplete_returns_none(conn):
    put_forecast(conn, MADE_AT, [50.0, 60.0])
    assert latest_forecast(conn, NOW, 5400) is None


# ---------- chạy một lần ----------

def test_main_exits_zero_without_data(tmp_path, monkeypatch):
    monkeypatch.setattr(forecast, "is_clock_trusted", lambda: True)
    db = tmp_path / "aiot.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"db_path: {db.as_posix()}\n", encoding="utf-8")
    assert forecast.main(["--config", str(cfg)]) == 0
    c = connect(db)
    assert c.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0] == 0
    c.close()


def test_main_skips_when_clock_not_synced(tmp_path, monkeypatch, caplog):
    """Khởi động lại trước khi có NTP: không lập dự báo với giờ sai (có thể bị coi là mới)."""
    db = tmp_path / "aiot.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"db_path: {db.as_posix()}\n", encoding="utf-8")
    called = []
    monkeypatch.setattr(forecast, "is_clock_trusted", lambda: False)
    monkeypatch.setattr(forecast, "run_forecast", lambda *a, **kw: called.append(a))
    with caplog.at_level(logging.INFO, logger="aiot.forecast"):
        assert forecast.main(["--config", str(cfg)]) == 0
    assert called == []
    assert "đồng bộ" in caplog.text
    monkeypatch.setattr(forecast, "is_clock_trusted", lambda: True)
    assert forecast.main(["--config", str(cfg)]) == 0
    assert len(called) == 1
