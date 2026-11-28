"""Kiểm tra xuất dữ liệu trạm theo giờ."""
import pytest

pd = pytest.importorskip("pandas")

from aiot.db import connect, init_schema
from training.export_hourly import export_hourly

H = 3600
BASE = 1_793_000_000 // H * H


def add_hour(conn, start, n, pm25, valid=1, offset=0):
    for i in range(n):
        conn.execute(
            "INSERT INTO readings (ts, pm1, pm25, pm10, temp, hum, lux, valid) "
            "VALUES (?, 1, ?, ?, 25, 70, 100, ?)",
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
