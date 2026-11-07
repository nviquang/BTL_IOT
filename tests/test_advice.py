"""Test lời khuyên Gemini: payload chỉ có số liệu tổng hợp, kiểm tra JSON trả về, mẫu cố định
khi lỗi. Không dùng mạng, không cần cài google-genai (client giả)."""
import dataclasses
import json
import sys
import time
from types import SimpleNamespace

import pytest

from aiot import advice
from aiot.advice import ALLOWED_KEYS, FORECAST_KEYS, Advice, get_advice, template_advice
from aiot.aqi import LABELS
from aiot.config import Config, GeminiCfg, Secrets

CFG = dataclasses.replace(Config(), gemini=GeminiCfg(model="test-model"),
                          secrets=Secrets(gemini_api_key="fake-key-not-real"))
GOOD = json.dumps({"headline": "Không khí trung bình",
                   "advice": "Người nhạy cảm nên hạn chế ra ngoài lâu."})


def summary(**over):
    s = {
        "period_hours": 24, "has_data": True, "data_coverage": 0.95,
        "pm25_mean": 20.0, "pm25_min": 8.0, "pm25_max": 40.0, "aqi_24h": "Trung bình",
        "pm25_last_1h_mean": 22.0, "aqi_now": "Trung bình",
        "aqi_hours": {label: 0 for label in LABELS},
        "temp_mean": 27.0, "hum_mean": 70.0, "mq135_exceeded_windows": 0,
        "fan_on_now": False, "control_mode": "BÌNH THƯỜNG", "manual_override": False,
        "fan_on_minutes_24h": 0,
        "forecast": {"model": "persistence", "made_at_local": "06:00",
                     "hours": ["06:00", "07:00", "08:00", "09:00", "10:00", "11:00"],
                     "pm25_next_6h": [22.0] * 6, "aqi_next_6h": ["Trung bình"] * 6},
    }
    s.update(over)
    return s


class FakeModels:
    def __init__(self, text=GOOD, exc=None, delay=0.0):
        self.text, self.exc, self.delay = text, exc, delay
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.delay:
            time.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(text=self.text)


class FakeClient:
    def __init__(self, **kw):
        self.models = FakeModels(**kw)


def test_good_reply_uses_gemini():
    client = FakeClient()
    a = get_advice(summary(), CFG, client=client)
    assert a == Advice("Không khí trung bình", "Người nhạy cảm nên hạn chế ra ngoài lâu.", "gemini")
    assert client.models.calls[0]["model"] == "test-model"


def test_payload_contains_only_aggregates():
    client = FakeClient()
    s = summary(chat_id=111, device_id="pi01", raw_samples=[(1, 2.0)], token="x")
    get_advice(s, CFG, client=client)
    sent = json.loads(client.models.calls[0]["contents"])
    assert set(sent) <= ALLOWED_KEYS
    assert set(sent["forecast"]) <= FORECAST_KEYS
    text = client.models.calls[0]["contents"] + json.dumps(client.models.calls[0]["config"],
                                                           default=str, ensure_ascii=False)
    for bad in ("chat_id", "device_id", "pi01", "raw_samples", "token", "fake-key"):
        assert bad not in text
    assert sent["pm25_mean"] == 20.0 and sent["aqi_now"] == "Trung bình"


def test_temperature_and_json_mode_requested():
    client = FakeClient()
    get_advice(summary(), CFG, client=client)
    config = client.models.calls[0]["config"]
    assert config["temperature"] == 0.2
    assert config["response_mime_type"] == "application/json"


def test_prompt_forbids_diagnosis_and_invented_numbers():
    client = FakeClient()
    get_advice(summary(), CFG, client=client)
    prompt = client.models.calls[0]["config"]["system_instruction"].lower()
    assert "chẩn đoán" in prompt
    assert "bịa" in prompt
    assert "headline" in prompt and "advice" in prompt
    assert "điều khiển" in prompt          # Gemini không điều khiển thiết bị


@pytest.mark.parametrize("exc", [RuntimeError("boom"), ConnectionError("network down"),
                                 ValueError("bad")])
def test_api_exception_uses_template(exc):
    a = get_advice(summary(), CFG, client=FakeClient(exc=exc))
    assert a.source == "template"
    assert a == template_advice(summary())


def test_timeout_uses_template():
    a = get_advice(summary(), CFG, client=FakeClient(exc=TimeoutError("timed out")))
    assert a.source == "template"
    # treo lâu hơn timeout_s: không chờ hết, dùng mẫu
    cfg = dataclasses.replace(CFG, gemini=dataclasses.replace(CFG.gemini, timeout_s=0.2))
    start = time.monotonic()
    a = get_advice(summary(), cfg, client=FakeClient(delay=2.0))
    assert a.source == "template"
    assert time.monotonic() - start < 1.5


def test_quota_error_uses_template():
    class ResourceExhausted(Exception):
        code = 429
    a = get_advice(summary(), CFG, client=FakeClient(exc=ResourceExhausted("429 RESOURCE_EXHAUSTED")))
    assert a.source == "template"


@pytest.mark.parametrize("text", ["không phải json", "", None, "[1, 2]", '"chuỗi"',
                                  '{"headline": "a", "advice": "b"'])
def test_invalid_json_uses_template(text):
    assert get_advice(summary(), CFG, client=FakeClient(text=text)).source == "template"


@pytest.mark.parametrize("data", [{"headline": "a"}, {"advice": "b"},
                                  {"headline": "", "advice": "b"},
                                  {"headline": "a", "advice": "   "},
                                  {"headline": 1, "advice": "b"},
                                  {"headline": "a", "advice": ["b"]}])
def test_missing_field_uses_template(data):
    reply = json.dumps(data)
    assert get_advice(summary(), CFG, client=FakeClient(text=reply)).source == "template"


def test_overlong_text_uses_template():
    reply = json.dumps({"headline": "Tiêu đề", "advice": "x" * 800})
    assert get_advice(summary(), CFG, client=FakeClient(text=reply)).source == "template"
    ok = json.dumps({"headline": "ab", "advice": "x" * 798})        # đúng 800 ký tự
    assert get_advice(summary(), CFG, client=FakeClient(text=ok)).source == "gemini"


def test_template_exists_for_every_aqi_level():
    seen = set()
    for label in LABELS:
        a = template_advice(summary(aqi_now=label))
        assert a.source == "template" and a.headline.strip() and a.advice.strip()
        assert "chẩn đoán" not in (a.headline + a.advice).lower()
        seen.add(a.advice)
    assert len(seen) == len(LABELS)
    missing = template_advice(summary(has_data=False, aqi_now=None, aqi_24h=None))
    assert "thiếu dữ liệu" in (missing.headline + missing.advice).lower()
    # không có giờ gần nhất thì theo nhóm TB 24 giờ
    assert template_advice(summary(aqi_now=None, aqi_24h="Xấu")) == \
        template_advice(summary(aqi_now="Xấu"))


def test_no_data_does_not_call_gemini():
    client = FakeClient()
    a = get_advice(summary(has_data=False, aqi_now=None, aqi_24h=None), CFG, client=client)
    assert a.source == "template" and not client.models.calls


def test_empty_model_name_uses_template():
    cfg = dataclasses.replace(CFG, gemini=GeminiCfg(model=""))
    client = FakeClient()
    assert get_advice(summary(), cfg, client=client).source == "template"
    assert not client.models.calls


def test_missing_key_or_sdk_uses_template(monkeypatch):
    no_key = dataclasses.replace(CFG, secrets=Secrets())
    assert get_advice(summary(), no_key).source == "template"
    # có key nhưng chưa cài google-genai: import lỗi -> mẫu cố định
    monkeypatch.setitem(sys.modules, "google", None)
    assert get_advice(summary(), CFG).source == "template"


def test_gemini_text_is_stripped():
    reply = json.dumps({"headline": "  Tiêu đề  ", "advice": " Lời khuyên. "})
    a = get_advice(summary(), CFG, client=FakeClient(text=reply))
    assert a == Advice("Tiêu đề", "Lời khuyên.", "gemini")
    assert advice.MAX_TEXT_CHARS == 800
