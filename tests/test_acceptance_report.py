"""Test script nghiệm thu: tỉ lệ dữ liệu hợp lệ, thời gian phản ứng của quạt, nhảy trạng thái,
báo cáo sáng đúng giờ, hiệu quả lọc và tương quan với trạm công khai. Không dùng mạng."""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import acceptance_report as ar  # noqa: E402
from aiot.db import RawSample, connect, init_schema, insert_raw, log_event  # noqa: E402
from aiot.notifier import enqueue, mark_sent  # noqa: E402

H = 3600
T0 = 1_793_000_000 // H * H          # đầu một giờ


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "aiot.db")
    init_schema(c)
    yield c
    c.close()


def add_reading(conn, ts, pm25, valid=1, hum=None):
    conn.execute("INSERT INTO readings (ts, pm25, hum, valid) VALUES (?, ?, ?, ?)",
                 (ts, pm25, hum, valid))
    conn.commit()


def add_raw(conn, ts, pm25):
    insert_raw(conn, RawSample(ts, None, pm25, None, None, None, None, None, True))


# ---------- ổn định ----------
def test_valid_ratio_counts_missing_windows(conn):
    start, end = T0, T0 + 24 * H
    for ts in range(start, start + 12 * H, 300):        # Pi chỉ chạy 12 giờ đầu
        add_reading(conn, ts, 20.0)
    add_reading(conn, start + 13 * H, 20.0, valid=0)    # cửa sổ không hợp lệ không tính
    add_reading(conn, end, 20.0)                        # ngoài khoảng [start, end)
    assert ar.valid_ratio(conn, start, end) == pytest.approx(0.5)


def test_valid_ratio_rejects_empty_range(conn):
    with pytest.raises(ValueError):
        ar.valid_ratio(conn, T0, T0 + 100)


# ---------- điều khiển ----------
def test_control_reaction_time_from_events(conn):
    # lần 1: thấp, rồi vượt 55 từ T0+100 liên tục; quạt bật lúc T0+760
    for ts in range(T0, T0 + 100, 10):
        add_raw(conn, ts, 20.0)
    for ts in range(T0 + 100, T0 + 800, 10):
        add_raw(conn, ts, 80.0)
    log_event(conn, T0 + 760, "fan_on", "enter")
    # lần 2: chuỗi vượt ngưỡng bị ngắt bởi một mẫu thấp -> tính từ sau mẫu thấp
    base = T0 + 2 * H
    for ts in range(base, base + 300, 10):
        add_raw(conn, ts, 90.0)
    add_raw(conn, base + 300, 40.0)
    for ts in range(base + 310, base + 1000, 10):
        add_raw(conn, ts, 90.0)
    log_event(conn, base + 960, "fan_on", "enter")
    # bật vì dự báo (proactive) và ghi đè không phải phản ứng với số đo: bỏ qua
    log_event(conn, T0 + 4 * H, "fan_on", "proactive")
    log_event(conn, T0 + 5 * H, "override", "set fan=on until=0")
    # bật nhưng không có mẫu thô nào trước đó (đã bị xoá): bỏ qua
    log_event(conn, T0 + 6 * H, "fan_on", "enter")
    assert ar.control_reaction_times(conn, T0, T0 + 7 * H) == [660.0, 650.0]


def test_control_reaction_time_breaks_run_on_data_gap(conn):
    for ts in range(T0, T0 + 200, 10):
        add_raw(conn, ts, 80.0)
    for ts in range(T0 + 500, T0 + 800, 10):          # mất dữ liệu 300 s > 120 s
        add_raw(conn, ts, 80.0)
    log_event(conn, T0 + 800, "fan_on", "danger")
    assert ar.control_reaction_times(conn, T0, T0 + H, enter_pm25=55.0) == [300.0]


def test_count_flaps(conn):
    log_event(conn, T0, "fan_on", "enter")
    log_event(conn, T0 + 1200, "fan_off", "exit")      # giữ BẢO VỆ 20 phút: đúng
    log_event(conn, T0 + 1500, "fan_on", "enter")      # vào lại 300 s sau khi thoát: hợp lệ
    log_event(conn, T0 + 1600, "fan_off", "exit")      # BẢO VỆ chỉ 100 s < min_state_s: lỗi
    log_event(conn, T0 + 1700, "fan_off", "exit")      # trùng loại, không phải đổi trạng thái
    log_event(conn, T0 + 1800, "fan_on", "enter")      # 'enter' sau 200 s < enter_hold_s: lỗi
    log_event(conn, T0 + 9000, "fan_off", "exit")
    log_event(conn, T0 + 9010, "fan_on", "danger")     # nguy hại vào lại ngay: hợp lệ
    log_event(conn, T0 + 9100, "danger", "x")          # loại khác không tính
    assert ar.count_flaps(conn, T0, T0 + 3 * H) == 2
    assert ar.count_flaps(conn, T0, T0 + 3 * H, min_state_s=60, enter_hold_s=60) == 0
    # vào lại nhanh (< min_state_s sau khi thoát) chỉ để tham khảo: 1500, 1800, 9010
    assert ar.quick_reentries(conn, T0, T0 + 3 * H) == 3


def test_count_flaps_seeds_state_from_before_start(conn):
    log_event(conn, T0 - 100, "fan_on", "enter")       # trước khoảng đo
    log_event(conn, T0 + 200, "fan_off", "exit")       # BẢO VỆ 300 s: lỗi, dù fan_on ở ngoài
    log_event(conn, T0 + 400, "fan_off", "exit")       # trùng loại
    assert ar.count_flaps(conn, T0, T0 + H) == 1
    # chỉ sự kiện trong [start, end) mới được đếm; sự kiện mồi không tự tính
    assert ar.count_flaps(conn, T0 + 300, T0 + H) == 0
    assert ar.count_flaps(conn, T0 - 200, T0 + 100) == 0


# ---------- Telegram ----------
def _at(day, hh, mm):
    return int(datetime(day.year, day.month, day.day, hh, mm).timestamp())


def test_report_punctuality_counts_6_of_7(conn):
    days = [date(2026, 11, 10) + timedelta(days=i) for i in range(7)]
    for i, d in enumerate(days):
        created = _at(d, 6, 30)
        enqueue(conn, "morning_report", f"báo cáo {i}", created)
        row_id = conn.execute("SELECT max(id) FROM outbox").fetchone()[0]
        if i == 3:
            mark_sent(conn, row_id, _at(d, 8, 0))       # mất mạng, gửi bù muộn 90 phút
        else:
            mark_sent(conn, row_id, created + 20 + i * 60)
    # trả lời lệnh /report không phải báo cáo sáng
    enqueue(conn, "reply", "báo cáo", _at(days[3], 6, 31))
    mark_sent(conn, conn.execute("SELECT max(id) FROM outbox").fetchone()[0], _at(days[3], 6, 31))
    assert ar.report_punctuality(conn, days) == (6, 7)


def test_report_punctuality_missing_and_unsent_days_count_as_late(conn):
    days = [date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12)]
    enqueue(conn, "morning_report", "a", _at(days[0], 6, 30))
    mark_sent(conn, 1, _at(days[0], 6, 31))
    enqueue(conn, "morning_report", "b", _at(days[1], 6, 30))      # chưa gửi
    assert ar.report_punctuality(conn, days) == (1, 3)
    # cho phép trễ 0 giây: 1 phút cũng là muộn
    assert ar.report_punctuality(conn, days, tolerance_s=0) == (0, 3)


def test_morning_report_details_reports_catch_up(conn):
    d = date(2026, 11, 10)
    enqueue(conn, "morning_report", "a", _at(d, 6, 30))
    mark_sent(conn, 1, _at(d, 9, 0))
    rows = ar.morning_report_details(conn, [d])
    assert rows == [(d, _at(d, 6, 30), _at(d, 9, 0), False)]


# ---------- hiệu quả lọc ----------
def test_fan_effect_compares_on_and_off_periods(conn):
    # quạt bật [T0, T0+1h): PM giảm 1 µg/m³ mỗi 5 phút; tắt [T0+1h, T0+2h): tăng 0,5
    log_event(conn, T0, "fan_on", "enter")
    log_event(conn, T0 + H, "fan_off", "exit")
    pm = 80.0
    for ts in range(T0, T0 + H, 300):
        add_reading(conn, ts, pm)
        pm -= 1.0
    for ts in range(T0 + H, T0 + 2 * H, 300):
        add_reading(conn, ts, pm)
        pm += 0.5
    out = ar.fan_effect(conn, T0, T0 + 2 * H)
    assert out["on_windows"] == 12 and out["off_windows"] == 12
    assert out["on_slope_per_h"] == pytest.approx(-12.0)
    assert out["off_slope_per_h"] == pytest.approx(6.0)
    assert out["on_mean"] == pytest.approx(74.5)
    assert out["off_mean"] == pytest.approx(68.0 + 0.5 * 5.5)


def test_fan_effect_without_data_returns_none_fields(conn):
    out = ar.fan_effect(conn, T0, T0 + H)
    assert out["on_windows"] == 0 and out["on_mean"] is None and out["on_slope_per_h"] is None


# ---------- cảm biến ----------
def test_station_correlation_after_linear_fit():
    local = [(T0 + i * H, 10.0 + (i * 7) % 40) for i in range(48)]
    public = [(ts, 2.0 * v + 3.0 + (0.5 if i % 2 else -0.5))
              for i, (ts, v) in enumerate(local)]
    public = public[5:] + [(T0 + 100 * H, 50.0)]         # chỉ giờ có ở cả hai mới tính
    out = ar.station_correlation(local, public)
    assert out["n"] == 43
    assert out["r"] > 0.99
    assert out["slope"] == pytest.approx(2.0, abs=0.05)
    assert out["intercept"] == pytest.approx(3.0, abs=1.0)
    assert out["mae_fit"] < 0.6 < out["mae_raw"]


def test_station_correlation_needs_overlap():
    with pytest.raises(ValueError):
        ar.station_correlation([(T0, 1.0), (T0 + H, 2.0)], [(T0, 1.0), (T0 + H, 2.0)])
    flat = [(T0 + i * H, 30.0) for i in range(5)]
    with pytest.raises(ValueError):
        ar.station_correlation(flat, [(ts, 10.0 + i) for i, (ts, _) in enumerate(flat)])


def test_local_hourly_requires_half_the_windows(conn):
    for i in range(6):
        add_reading(conn, T0 + i * 300, 10.0 + i)            # 6/12 cửa sổ: đủ
    for i in range(5):
        add_reading(conn, T0 + H + i * 300, 50.0)            # 5/12: thiếu
    assert ar.local_hourly(conn, T0, T0 + 2 * H) == [(T0, pytest.approx(12.5))]


def test_load_public_csv(tmp_path):
    p = tmp_path / "station.csv"
    p.write_text("timestamp,pm25,temp\n2026-11-10 06:00:00,30.5,20\n"
                 "2026-11-10 07:00:00,,21\n2026-11-10T08:00,41,22\n", encoding="utf-8")
    rows = ar.load_public_csv(p)
    assert rows == [(int(datetime(2026, 11, 10, 6).timestamp()), 30.5),
                    (int(datetime(2026, 11, 10, 8).timestamp()), 41.0)]


def test_main_prints_report(conn, tmp_path, capsys):
    db = tmp_path / "aiot.db"
    for ts in range(T0, T0 + H, 300):
        add_reading(conn, ts, 20.0, hum=70.0)
    start = datetime.fromtimestamp(T0).strftime("%Y-%m-%d %H:%M")
    end = datetime.fromtimestamp(T0 + 2 * H).strftime("%Y-%m-%d %H:%M")
    assert ar.main(["--db", str(db), "--start", start, "--end", end]) == 0
    out = capsys.readouterr().out
    assert "50,0%" in out                 # 12/24 cửa sổ hợp lệ
    assert "Báo cáo sáng" in out


def test_main_missing_db_exits_without_creating_file(tmp_path, capsys):
    db = tmp_path / "go_sai.db"
    assert ar.main(["--db", str(db)]) == 2
    assert not db.exists()
    assert "go_sai.db" in capsys.readouterr().err


def test_main_opens_db_read_only(conn, tmp_path, monkeypatch):
    db = tmp_path / "aiot.db"
    seen = []
    real_connect = ar.sqlite3.connect

    def spy(target, *a, **kw):
        seen.append((str(target), kw.get("uri")))
        return real_connect(target, *a, **kw)

    monkeypatch.setattr(ar.sqlite3, "connect", spy)
    monkeypatch.setattr(ar, "init_schema", lambda c: pytest.fail("không được ghi schema"),
                        raising=False)
    end = datetime.fromtimestamp(T0 + H).strftime("%Y-%m-%d %H:%M")
    start = datetime.fromtimestamp(T0).strftime("%Y-%m-%d %H:%M")
    assert ar.main(["--db", str(db), "--start", start, "--end", end]) == 0
    assert seen and seen[0][0].startswith("file:") and seen[0][0].endswith("?mode=ro")
    assert seen[0][1] is True
