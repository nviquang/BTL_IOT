"""
Dịch vụ controller: mỗi loop_s giây đọc PM2.5 từ SQLite, chạy luật decide()
và bật/tắt quạt qua relay GPIO23.

- Chỉ dịch vụ này chạm GPIO23. Lệnh từ web (aiot.uplink qua aiot.commands) ghi khoá state
  'pending_override' dạng JSON:
  {"fan": true|false, "ttl_s": int, "at": epoch} đặt ghi đè; {"fan": null, "at": epoch}
  xoá ghi đè (chế độ tự động). Controller đọc khoá ở đầu mỗi bước; trạng thái mới, sự kiện
  'override' và lệnh xoá khoá (chỉ khi giá trị chưa đổi) được ghi trong cùng một transaction.
  Lệnh thiếu "at", hoặc lệch quá control.stale_command_s + control.loop_s so với giờ hiện
  tại (cũ, hay "ở tương lai"), bị bỏ với sự kiện 'override_stale': controller dừng vài giờ
  thì lệnh quạt cũ không được chạy khi nó chạy lại.
- Mỗi bước ghi khoá 'controller_heartbeat' (giây epoch) cùng transaction; healthcheck và
  uplink dùng khoá này để biết controller còn chạy.
- Trạng thái logic (ControlState) lưu ở khoá 'control_state' sau mỗi bước, trước khi
  ghi relay. Relay được ghi lại mỗi vòng (idempotent), nên relay lỗi thì vòng sau tự
  thử lại; sự kiện 'relay_error' chỉ ghi một lần cho cả chuỗi lỗi.
- Khởi động: khôi phục trạng thái cuối trong DB (thiếu: BÌNH THƯỜNG; hỏng: BÌNH THƯỜNG
  và ghi 'state_corrupt'; ghi đè hết hạn lúc Pi tắt thì xoá; xoá mốc enter_since/exit_since
  vì điều kiện không được theo dõi trong lúc dừng) rồi đưa quạt về đúng mức.
- Sau khi Pi khởi động lại: chỉ dùng mẫu thô có ts >= boot_wall = now - time.monotonic()
  (mẫu trước khi tắt máy không lọt vào cửa sổ 10/15 phút khi đồng hồ được khôi phục gần
  giờ tắt). Trong stale_s giây đầu sau khi khởi động, thiếu dữ liệu PM vẫn giữ nguyên trạng
  thái nhưng không báo 'sensor_fault' (collector chưa kịp ghi mẫu đầu); quá thời gian đó mà
  vẫn thiếu thì báo như thường.
- Relay lỗi: sự kiện 'relay_error' chỉ ghi một lần cho cả chuỗi lỗi, 'relay_recovered' khi
  ghi được trở lại. Mọi sự kiện nằm trong bảng events (web hiện ở nhật ký).
- Dự báo (Task 9): mỗi bước đọc latest_forecast (TB 3 giờ tới của dự báo mới nhất, bỏ
  qua nếu cũ hơn control.forecast_max_age_s) cho luật 'Chủ động'. Đọc lỗi thì coi như
  không có dự báo, ghi log một lần, vẫn điều khiển bình thường.
- Còi (aiot/alarm.py, aiot/buzzer.py, GPIO5): trung vị PM2.5 1 phút >= buzzer.alarm_pm25
  thì kêu một hồi, lặp lại mỗi buzzer.repeat_s giây khi còn vượt, tắt ngay khi hết. Khoá 'buzzer_mute_until' (nếu có) tắt tiếng tới mốc đó. Trạng
  thái còi lưu ở khoá 'alarm_state'. Lỗi còi chỉ ghi log, không ảnh hưởng quạt.
  Lệnh còi thủ công (web qua aiot.uplink) nằm ở khoá 'pending_buzzer'; controller
  kiểm tra tuổi lệnh như pending_override rồi ghi 'buzzer_override' {"on", "until", "at"}
  trong cùng transaction của bước (bật tối đa buzzer.manual_max_s; null = xoá ghi đè).
- Nhận diện nguồn bụi (aiot/source_model.py, mục source trong config.yaml): mỗi bước đọc
  fast_readings 30 phút (nền) và tính đặc trưng cửa sổ 120 s (aiot/source_window.py), dự đoán
  bằng logistic regression, làm mượt (khoá 'source_state') và ghi source_predictions (kèm
  điểm bất thường 'anomaly' = 1 − P(normal)) cùng transaction của bước. Có nhãn: quạt và còi
  theo nhãn (nấu ăn: quạt; ô nhiễm bụi mịn: quạt + còi; bình thường: tắt cả hai), đổi nhãn
  thì ghi sự kiện 'source_change'. Không có mô hình, mẫu 1 giây cũ hơn
  source.stale_s hoặc chưa đủ tin cậy: luật ngưỡng PM2.5 như trước. Lỗi ở phần này chỉ
  ghi log và quay về luật ngưỡng.
- SIGTERM/SIGINT: đưa GPIO23 về LOW, tắt còi, nhả chip, thoát 0.
- Không phụ thuộc Internet.

Chạy: cd ~/BTL_IOT && venv/bin/python -m aiot.controller [--config demo.yaml]
"""
import argparse
import dataclasses
import json
import logging
import signal
import threading
import time
from datetime import datetime
from pathlib import Path

from aiot.alarm import BURST, SILENCE, AlarmState, alarm_reason, is_muted, plan, plan_manual
from aiot.commands import BUZZER_OVERRIDE_KEY, BUZZER_PENDING_KEY, buzzer_override
from aiot.config import BuzzerCfg, SourceCfg
from aiot.control import ControlState, Event, apply_override, decide, fan_on, inputs_from_series
from aiot.db import (connect, delete_state, fast_rows, get_state, init_schema, insert_prediction,
                     log_event, pm25_series, set_state)
from aiot.forecast import latest_forecast

log = logging.getLogger("aiot.controller")

STATE_KEY = "control_state"
PENDING_KEY = "pending_override"
RELAY_FAULT_KEY = "relay_fault"     # "1" khi đang trong chuỗi lỗi relay
HEARTBEAT_KEY = "controller_heartbeat"   # giây epoch của bước gần nhất (healthcheck, uplink đọc)
ALARM_KEY = "alarm_state"           # AlarmState của còi (JSON)
SOURCE_KEY = "source_state"         # SourceState của bộ nhận diện nguồn bụi (JSON)
MUTE_KEY = "buzzer_mute_until"      # giây epoch hết tắt tiếng còi (nếu có)

_forecast_error_logged = False      # lỗi đọc dự báo chỉ ghi log một lần mỗi chuỗi lỗi


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---------------- Đọc/ghi trạng thái ----------------
def _load_state(conn):
    """(trạng thái đã lưu, danh sách Event). Thiếu thì mặc định; hỏng thì mặc định
    kèm Event 'state_corrupt'. Không ghi gì: người gọi ghi cùng transaction của bước."""
    text = get_state(conn, STATE_KEY)
    if text is None:
        return ControlState(), []
    try:
        return ControlState.from_json(text), []
    except ValueError as e:
        log.error("Trạng thái điều khiển hỏng, về BÌNH THƯỜNG: %s", e)
        return ControlState(), [Event("state_corrupt", str(e))]


def _parse_pending(text, default_ttl_s: int):
    """(fan, ttl_s, at) từ JSON pending_override; sai định dạng thì ValueError.
    at (giây epoch lúc lệnh được ghi) là None khi thiếu hoặc không phải số nguyên."""
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("không phải JSON") from None
    if not isinstance(data, dict) or "fan" not in data:
        raise ValueError("thiếu khoá 'fan'")
    fan = data["fan"]
    if fan is not None and not isinstance(fan, bool):
        raise ValueError("'fan' phải là true/false/null")
    ttl_s = data.get("ttl_s", default_ttl_s)
    if fan is not None and (not isinstance(ttl_s, int) or isinstance(ttl_s, bool) or ttl_s <= 0):
        raise ValueError("'ttl_s' phải là số nguyên dương")
    at = data.get("at")
    if not isinstance(at, int) or isinstance(at, bool):
        at = None
    return fan, ttl_s, at


def _apply_pending(st: ControlState, text: str, cfg, now: int, max_age_s: int):
    """Áp dụng lệnh ghi đè (từ web) vào st (trong bộ nhớ). Trả về (st, [Event]).
    Lệnh thiếu "at" hoặc lệch quá max_age_s giây so với now bị bỏ ('override_stale')."""
    try:
        fan, ttl_s, at = _parse_pending(text, cfg.override_ttl_s)
    except ValueError as e:
        log.warning("Bỏ qua pending_override sai định dạng: %s", e)
        return st, [Event("override_invalid", f"{e}: {text[:200]}")]
    if at is None or abs(now - at) > max_age_s:
        age = "no 'at'" if at is None else f"at={at} now={now} max_age_s={max_age_s}"
        log.warning("Bỏ lệnh ghi đè cũ hoặc sai giờ (%s)", age)
        return st, [Event("override_stale", f"{age}: {text[:200]}")]
    st = apply_override(st, fan, now, ttl_s)
    if fan is None:
        detail = "cleared (auto)"
    else:
        detail = (f"set fan={'on' if fan else 'off'} until={st.override_until} "
                  f"({_fmt_ts(st.override_until)})")
    log.info("Ghi đè thủ công: %s", detail)
    return st, [Event("override", detail)]


_KEEP = object()    # lệnh còi bị bỏ: không đổi khoá buzzer_override


def _apply_buzzer_pending(text: str, now: int, max_age_s: int, max_on_s: int):
    """Lệnh còi trong 'pending_buzzer' -> (giá trị mới của 'buzzer_override': JSON, None =
    xoá, _KEEP = giữ nguyên; [Event]). Lệnh sai định dạng hoặc cũ bị bỏ kèm sự kiện."""
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or "on" not in data:
            raise ValueError("thiếu khoá 'on'")
        on = data["on"]
        if on is not None and not isinstance(on, bool):
            raise ValueError("'on' phải là true/false/null")
        ttl_s = data.get("ttl_s")
        if on is not None and (not isinstance(ttl_s, int) or isinstance(ttl_s, bool) or ttl_s <= 0):
            raise ValueError("'ttl_s' phải là số nguyên dương")
    except ValueError as e:
        log.warning("Bỏ qua pending_buzzer sai định dạng: %s", e)
        return _KEEP, [Event("buzzer_override_invalid", f"{e}: {text[:200]}")]
    at = data.get("at")
    if not isinstance(at, int) or isinstance(at, bool) or abs(now - at) > max_age_s:
        log.warning("Bỏ lệnh còi cũ hoặc sai giờ: %s", text[:200])
        return _KEEP, [Event("buzzer_override_stale", text[:200])]
    if on is None:
        return None, [Event("buzzer_override", "cleared (auto)")]
    if on:
        ttl_s = min(ttl_s, max_on_s)
    until = now + ttl_s
    value = json.dumps({"on": on, "until": until, "at": now}, sort_keys=True)
    detail = f"set buzzer={'on' if on else 'off'} until={until} ({_fmt_ts(until)})"
    log.info("Ghi đè còi: %s", detail)
    return value, [Event("buzzer_override", detail)]


def _persist(conn, now: int, st: ControlState, events, pending_text=None, extra=None) -> None:
    """Ghi control_state, mọi Event và (nếu có) xoá đúng lệnh pending đã đọc trong MỘT
    transaction, commit một lần. Lỗi ghi DB thì rollback toàn bộ và raise: lệnh pending còn
    nguyên cho vòng sau, không có sự kiện nào được ghi mà không áp dụng."""
    try:
        set_state(conn, STATE_KEY, st.to_json(), commit=False)
        set_state(conn, HEARTBEAT_KEY, int(now), commit=False)
        for ev in events:
            log.info("Sự kiện %s %s", ev.kind, ev.detail)
            log_event(conn, now, ev.kind, ev.detail, commit=False)
        if pending_text is not None:
            # Chỉ xoá nếu chưa có lệnh mới kể từ lúc đọc
            delete_state(conn, PENDING_KEY, value=pending_text, commit=False)
        if extra is not None:
            extra()
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _drive(conn, relay, on: bool, now: int) -> bool:
    """Ghi mức relay. Lỗi thì ghi sự kiện 'relay_error' một lần mỗi chuỗi lỗi, không raise."""
    try:
        relay.set(on)
    except Exception as e:
        log.error("Không ghi được relay (%s): %s", "bật" if on else "tắt", e)
        if get_state(conn, RELAY_FAULT_KEY) != "1":
            try:
                log_event(conn, now, "relay_error", f"set {'on' if on else 'off'}: {e}", commit=False)
                set_state(conn, RELAY_FAULT_KEY, "1", commit=False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return False
    if get_state(conn, RELAY_FAULT_KEY) == "1":
        log.info("Relay hoạt động lại")
        try:
            log_event(conn, now, "relay_recovered", f"set {'on' if on else 'off'}", commit=False)
            delete_state(conn, RELAY_FAULT_KEY, commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return True


def _read_forecast(conn, cfg, now: int):
    """(TB 3 giờ tới, made_at) hoặc (None, None). Không bao giờ raise."""
    global _forecast_error_logged
    try:
        found = latest_forecast(conn, now, cfg.forecast_max_age_s)
    except Exception as e:
        if not _forecast_error_logged:
            log.warning("Không đọc được dự báo, điều khiển không có dự báo: %s", e)
            _forecast_error_logged = True
        return None, None
    if _forecast_error_logged:
        log.info("Đọc được dự báo trở lại")
        _forecast_error_logged = False
    return found if found is not None else (None, None)


def _classify(conn, model, scfg: SourceCfg, now: int, since_ts: int):
    """(nhãn AI đã làm mượt hoặc None, [Event], hàm ghi kết quả vào DB không commit).

    Không bao giờ raise: lỗi thì ghi log, trả (None, [], None) và controller dùng luật
    ngưỡng PM2.5."""
    if model is None or not scfg.enabled:
        return None, [], None
    from aiot.source_model import SourceState, smooth
    from aiot.source_window import BASE_S, buffer_from_rows
    try:
        rows = fast_rows(conn, max(now - BASE_S + 1, since_ts), now)
        proba = None
        raw_label = None
        if rows and now - rows[-1][0] <= scfg.stale_s:
            # cửa sổ kết thúc ở MẪU MỚI NHẤT đã có trong DB, không phải giây hiện tại: collector
            # chỉ commit fast_readings mỗi FAST_FLUSH_S = 5 s, nên vài giây cuối thường chưa có;
            # cắt theo `now` làm khối 5 giây mới nhất rỗng và nhãn nhảy theo nhịp ghi
            f = model.featurize(buffer_from_rows(rows, rows[-1][0]))
            if f is not None:
                p = model.predict_proba(f)
                proba = p
                raw_label = model.classes[int(p.argmax())]
        old = SourceState.from_json(get_state(conn, SOURCE_KEY))
        new = smooth(old, proba, now, vote_n=scfg.vote_n, min_confidence=scfg.min_confidence,
                     max_gap_s=scfg.max_gap_s, classes=model.classes)
        events = []
        if new.label is not None and new.label != old.label:
            conf = float(sum(h[_label_index(new.label)] for h in new.history) / len(new.history))
            events.append(Event("source_change", f"{new.label} p={conf:.2f}"))
        proba_json = None
        if proba is not None:
            pj = {c: round(float(v), 3) for c, v in zip(model.classes, proba)}
            pj["anomaly"] = round(model.anomaly_score(proba), 3)    # 1 − P(normal)
            proba_json = json.dumps(pj)

        def write():
            set_state(conn, SOURCE_KEY, new.to_json(), commit=False)
            insert_prediction(conn, now, new.label, raw_label, proba_json, model.name, commit=False)
        return new.label, events, write
    except Exception:
        log.exception("Lỗi bộ nhận diện nguồn bụi, dùng luật ngưỡng PM2.5 vòng này")
        try:
            conn.rollback()
        except Exception:
            pass
        return None, [], None


def _label_index(label):
    from aiot.source_model import LABELS
    return LABELS.index(label)


def load_source_model(scfg: SourceCfg):
    """Nạp mô hình nguồn bụi; thiếu file hay hỏng thì None (dùng luật ngưỡng)."""
    if not scfg.enabled:
        log.info("Bộ nhận diện nguồn bụi đang tắt trong config.yaml")
        return None
    try:
        from aiot.source_model import load_model
        model = load_model(scfg.model_path)
        log.info("Đã nạp mô hình nguồn bụi %s (%s, %d đặc trưng)", scfg.model_path, model.name,
                 len(model.features))
        return model
    except Exception as e:
        log.error("Không nạp được mô hình nguồn bụi %s, dùng luật ngưỡng PM2.5: %s",
                  scfg.model_path, e)
        return None


def _sound_alarm(conn, buzzer, bcfg: BuzzerCfg, inp, now: int) -> None:
    """Quyết định và ra lệnh cho còi. Không bao giờ raise: còi lỗi không được làm hỏng
    vòng điều khiển quạt."""
    try:
        st = AlarmState.from_json(get_state(conn, ALARM_KEY))
        ov = buzzer_override(conn, now)
        if ov is not None and ov[0]:
            new, duration = plan_manual(st, ov[2], ov[1], now, bcfg.manual_max_s)
            if new != st:
                set_state(conn, ALARM_KEY, new.to_json(), commit=False)
                log.info("Còi thủ công: kêu %d giây", duration)
                log_event(conn, now, "buzzer", f"manual {duration}s", commit=False)
                conn.commit()
            if duration:
                buzzer.burst(bcfg.beep_on_s, bcfg.beep_off_s, duration)
            return
        reason = alarm_reason(inp.danger_level, bcfg, source=inp.source)
        muted = is_muted(get_state(conn, MUTE_KEY), now) or ov is not None
        new, action = plan(st, reason, now, bcfg, muted)
        if new != st:
            set_state(conn, ALARM_KEY, new.to_json(), commit=False)
            if action == BURST and new.reason != st.reason:
                level = inp.danger_level
                detail = new.reason if level is None else f"{new.reason} pm25_1m={level:.0f}"
                if inp.source is not None:
                    detail += " (AI)"
                log.info("Còi báo vượt ngưỡng: %s", detail)
                log_event(conn, now, "buzzer", detail, commit=False)
            conn.commit()
        if action == BURST:
            buzzer.burst(bcfg.beep_on_s, bcfg.beep_off_s, bcfg.burst_s)
        elif action == SILENCE:
            buzzer.silence()
    except Exception:
        log.exception("Lỗi điều khiển còi, bỏ qua vòng này")
        try:
            conn.rollback()
        except Exception:
            pass


# ---------------- Khôi phục và một bước ----------------
def restore_state(conn, now: int) -> ControlState:
    """Trạng thái sau khởi động lại (đã lưu lại DB), chưa chạm relay.

    Xoá enter_since/exit_since: trong lúc dừng không ai theo dõi điều kiện, nên mốc cũ
    không còn nghĩa "đúng liên tục"; phải chờ đủ thời gian từ đầu. mode và entered_at giữ."""
    now = int(now)
    st, events = _load_state(conn)
    st = dataclasses.replace(st, enter_since=None, exit_since=None)
    if st.override_fan is not None and (st.override_until is None or now >= st.override_until):
        log.info("Ghi đè đã hết hạn trong lúc dịch vụ dừng, về tự động")
        st = apply_override(st, None, now, 0)
        events.append(Event("override_expired", "during downtime"))
    _persist(conn, now, st, events)
    return st


def restore(conn, relay, cfg, now: int) -> ControlState:
    """Khôi phục trạng thái rồi đưa relay về đúng mức ngay."""
    st = restore_state(conn, now)
    _drive(conn, relay, fan_on(st, int(now)), int(now))
    return st


def step(conn, relay, cfg, now: int, *, monotonic=time.monotonic, buzzer=None,
         bcfg: BuzzerCfg | None = None, model=None, scfg: SourceCfg | None = None) -> ControlState:
    """Một vòng điều khiển. cfg là ControlCfg, now là giây epoch, monotonic() là số giây từ
    lúc Pi khởi động (time.monotonic trên Linux). buzzer None thì không điều khiển còi.

    Đọc hết rồi mới ghi: trạng thái mới, sự kiện và việc xoá pending_override cùng một
    transaction (_persist), sau đó mới ghi relay."""
    now = int(now)
    st, events = _load_state(conn)
    pending_text = get_state(conn, PENDING_KEY)
    if pending_text is not None:
        max_age_s = cfg.stale_command_s + cfg.loop_s
        st, pending_events = _apply_pending(st, pending_text, cfg, now, max_age_s)
        events += pending_events
    bcfg = bcfg if bcfg is not None else BuzzerCfg()
    buzzer_text = get_state(conn, BUZZER_PENDING_KEY)
    buzzer_value = _KEEP
    if buzzer_text is not None:
        buzzer_value, buzzer_events = _apply_buzzer_pending(
            buzzer_text, now, cfg.stale_command_s + cfg.loop_s, bcfg.manual_max_s)
        events += buzzer_events

    uptime = monotonic()
    boot_wall = int(now - uptime)       # giờ khởi động theo đồng hồ hiện tại
    lookback = max(cfg.enter_mean_s, cfg.exit_mean_s, cfg.danger_window_s) + 60
    series = pm25_series(conn, max(now - lookback, boot_wall))
    forecast_3h, forecast_made_at = _read_forecast(conn, cfg, now)
    source, source_events, source_write = _classify(
        conn, model, scfg if scfg is not None else SourceCfg(), now, boot_wall)
    inp = inputs_from_series(series, now, cfg, forecast_3h=forecast_3h,
                             forecast_made_at=forecast_made_at, source=source)
    fault_before = st.fault_active
    st, decide_events = decide(st, inp, cfg)
    if uptime < cfg.stale_s and any(ev.kind == "sensor_fault" for ev in decide_events):
        # Vừa khởi động, collector chưa kịp ghi mẫu đầu: vẫn giữ nguyên trạng thái (decide
        # đã xoá bộ đếm) nhưng không báo lỗi; hết stale_s mà vẫn thiếu thì decide báo lại
        decide_events = [ev for ev in decide_events if ev.kind != "sensor_fault"]
        st = dataclasses.replace(st, fault_active=fault_before)
    events += decide_events
    events += source_events

    def extra():
        if source_write is not None:
            source_write()
        if buzzer_text is not None:
            if buzzer_value is None:
                delete_state(conn, BUZZER_OVERRIDE_KEY, commit=False)
            elif buzzer_value is not _KEEP:
                set_state(conn, BUZZER_OVERRIDE_KEY, buzzer_value, commit=False)
            delete_state(conn, BUZZER_PENDING_KEY, value=buzzer_text, commit=False)

    # Lưu trạng thái logic trước khi ghi relay: relay lỗi không làm mất quyết định
    _persist(conn, now, st, events, pending_text, extra=extra)
    level = fan_on(st, now)
    _drive(conn, relay, level, now)
    if buzzer is not None:
        _sound_alarm(conn, buzzer, bcfg, inp, now)
    return st


# ---------------- Vòng lặp dịch vụ ----------------
def run(conn, relay, cfg, stop: threading.Event, *, wall=time.time, clock=time.monotonic,
        buzzer=None, bcfg: BuzzerCfg | None = None, model=None,
        scfg: SourceCfg | None = None) -> None:
    """Gọi step mỗi cfg.loop_s giây tới khi stop được đặt. Lỗi một vòng không dừng dịch vụ;
    khi đó relay giữ nguyên mức cũ."""
    next_time = clock()
    while not stop.is_set():
        try:
            step(conn, relay, cfg, int(wall()), buzzer=buzzer, bcfg=bcfg, model=model, scfg=scfg)
        except Exception:
            log.exception("Lỗi trong vòng điều khiển, giữ nguyên quạt")
            try:
                conn.rollback()
            except Exception as e:
                log.error("Rollback lỗi: %s", e)

        next_time += cfg.loop_s
        delay = next_time - clock()
        if delay > 0:
            stop.wait(delay)
        else:
            next_time = clock()


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from aiot.buzzer import Buzzer
    from aiot.config import load_config
    from aiot.relay import Relay

    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Dịch vụ điều khiển quạt lọc qua relay GPIO23")
    parser.add_argument("--config", default=str(root / "config.yaml"),
                        help="file cấu hình (mặc định config.yaml trong thư mục repo)")
    args = parser.parse_args(argv)

    cfg = load_config(args.config, root / ".env")
    ctrl = cfg.control

    stop = threading.Event()

    def on_signal(signum, frame):
        log.info("Nhận tín hiệu %s, đang dừng", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    conn = connect(cfg.db_path)
    init_schema(conn)
    now = int(time.time())
    st = restore_state(conn, now)
    level = fan_on(st, now)
    # Nhận chân với mức đã khôi phục để quạt không tắt một nhịp khi khởi động lại
    relay = Relay(pin=23, initial=level)
    _drive(conn, relay, level, now)
    buzzer = None
    if cfg.buzzer.enabled:
        try:
            buzzer = Buzzer(pin=cfg.buzzer.gpio, active_low=cfg.buzzer.active_low)
            log.info("Còi trên GPIO%d (kích mức %s)", cfg.buzzer.gpio,
                     "thấp" if cfg.buzzer.active_low else "cao")
        except Exception as e:
            log.error("Không nhận được chân còi GPIO%d, chạy không có còi: %s", cfg.buzzer.gpio, e)
    log.info("Controller bắt đầu: db=%s mode=%s quạt=%s demo=%s loop=%ss",
             cfg.db_path, st.mode, "bật" if level else "tắt", ctrl.demo, ctrl.loop_s)
    try:
        model = load_source_model(cfg.source)
        run(conn, relay, ctrl, stop, buzzer=buzzer, bcfg=cfg.buzzer, model=model, scfg=cfg.source)
    finally:
        if buzzer is not None:
            try:
                buzzer.close()
            except Exception as e:
                log.error("Không tắt được còi khi dừng: %s", e)
        try:
            relay.close()               # GPIO23 về LOW, nhả chip
            log.info("Đã tắt quạt (GPIO23 LOW) và nhả GPIO")
        except Exception as e:
            log.error("Không tắt được relay khi dừng: %s", e)
        try:
            log_event(conn, int(time.time()), "controller_stop", "fan off")
        except Exception as e:
            log.error("Không ghi được sự kiện dừng: %s", e)
        conn.close()
        log.info("Controller đã dừng")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
