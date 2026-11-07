"""
Lời khuyên cho báo cáo không khí: Gemini viết, mẫu cố định thay thế khi có lỗi.

- An toàn AI: Gemini chỉ viết lời khuyên dạng chữ, không điều khiển thiết bị. Quạt do luật
  tất định trong aiot.control quyết định; module này không đọc hay ghi trạng thái quạt.
- Chỉ gửi số liệu đã tổng hợp (dict của aiot.report.summarize), lọc qua ALLOWED_KEYS.
  Không gửi device id, chat id, token hay mẫu thô.
- Yêu cầu Gemini trả JSON {headline, advice}, temperature lấy từ cấu hình (0.2).
- Mọi lỗi (thiếu model/API key/thư viện, lỗi mạng, hết hạn mức, quá timeout_s, JSON sai,
  thiếu trường, quá MAX_TEXT_CHARS ký tự) -> lời khuyên mẫu theo nhóm AQI hiện tại, để bản
  tin vẫn gửi được. Không có số liệu thì không gọi Gemini.
- Giới hạn số lần gọi mỗi ngày nằm ở aiot.report (cần DB).

Thư viện google-genai chỉ được import trong get_advice: chưa cài thì dùng mẫu cố định.
"""
import json
import logging
import threading
from dataclasses import dataclass
from typing import Literal

from aiot.aqi import LABELS

log = logging.getLogger("aiot.advice")

MAX_TEXT_CHARS = 800            # tổng độ dài headline + advice

# Khoá được phép gửi lên Gemini (danh sách trắng). summarize() chỉ tạo các khoá này.
ALLOWED_KEYS = frozenset({
    "period_hours", "has_data", "data_coverage",
    "pm25_mean", "pm25_min", "pm25_max", "aqi_24h",
    "pm25_last_1h_mean", "aqi_now", "aqi_hours",
    "temp_mean", "hum_mean", "mq135_exceeded_windows",
    "fan_on_now", "control_mode", "manual_override", "fan_on_minutes_24h",
    "forecast",
})
FORECAST_KEYS = frozenset({"model", "made_at_local", "hours", "pm25_next_6h", "aqi_next_6h"})

SYSTEM_INSTRUCTION = """\
Bạn viết lời khuyên ngắn bằng tiếng Việt cho bản tin chất lượng không khí trong nhà của một \
hộ gia đình ở Hà Nội. Dữ liệu là JSON số liệu đã tổng hợp từ cảm biến trong nhà: PM2.5 \
(µg/m³, nhãn AQI theo US EPA 2024), nhiệt độ, độ ẩm, số cửa sổ 5 phút cảm biến khí MQ135 \
báo vượt ngưỡng (chỉ là tín hiệu phụ, không phải nồng độ CO2), trạng thái quạt lọc và dự báo \
PM2.5 6 giờ tới (có thể là null).

Quy tắc bắt buộc:
- Không chẩn đoán y tế, không nêu tên bệnh, không kê thuốc. Chỉ đưa biện pháp phòng ngừa \
chung (đóng/mở cửa sổ, hạn chế vận động mạnh ngoài trời, khẩu trang chống bụi mịn, chú ý \
người nhạy cảm). Nếu cần, khuyên hỏi ý kiến nhân viên y tế.
- Không bịa số liệu: chỉ dùng con số có trong dữ liệu, không tự thêm số hay nguồn khác. \
Thiếu số liệu nào thì không nhắc tới nó.
- Quạt lọc do luật tự động trên thiết bị điều khiển. Không ra lệnh và không hứa điều khiển \
thiết bị; chỉ được mô tả trạng thái quạt có trong dữ liệu.
- Trả về đúng một object JSON: {"headline": "<tiêu đề, tối đa 80 ký tự>", \
"advice": "<2-4 câu, tối đa 500 ký tự>"}. Chữ thường, không Markdown, không emoji."""

_TEMPLATES = {
    "Tốt": ("Không khí trong nhà tốt",
            "Bụi mịn PM2.5 ở mức tốt. Có thể mở cửa sổ cho thoáng và sinh hoạt bình thường."),
    "Trung bình": ("Không khí ở mức trung bình",
                   "Phần lớn mọi người sinh hoạt bình thường. Người nhạy cảm với bụi nên hạn chế "
                   "ở lâu ngoài trời nơi đông xe và theo dõi bản tin tiếp theo."),
    "Kém": ("Không khí kém với người nhạy cảm",
            "Trẻ nhỏ, người cao tuổi và người có bệnh hô hấp nên giảm vận động mạnh ngoài trời. "
            "Đóng bớt cửa sổ khi ngoài đường nhiều bụi."),
    "Xấu": ("Không khí xấu",
            "Hạn chế ra ngoài và vận động mạnh, đóng cửa sổ, đeo khẩu trang chống bụi mịn khi ra "
            "đường. Quạt lọc tự bật theo luật khi PM2.5 cao kéo dài."),
    "Rất xấu": ("Không khí rất xấu",
                "Nên ở trong nhà, đóng kín cửa sổ và tránh vận động mạnh. Khi phải ra ngoài hãy "
                "đeo khẩu trang chống bụi mịn. Ai thấy khó chịu bất thường nên hỏi ý kiến nhân "
                "viên y tế."),
    "Nguy hại": ("Không khí nguy hại",
                 "Tránh ra ngoài, đóng kín cửa và để quạt lọc chạy. Đặc biệt chú ý trẻ nhỏ, người "
                 "cao tuổi và người có bệnh hô hấp; thấy khó chịu bất thường thì liên hệ cơ sở "
                 "y tế."),
}
_MISSING = ("Thiếu dữ liệu không khí",
            "Không có đủ số liệu PM2.5 hợp lệ trong 24 giờ qua nên chưa có lời khuyên theo số "
            "liệu. Kiểm tra dịch vụ collector và cảm biến PMS7003; trong lúc chờ, tham khảo bản "
            "tin chất lượng không khí công khai.")
assert set(_TEMPLATES) == set(LABELS)


@dataclass(frozen=True)
class Advice:
    headline: str
    advice: str
    source: Literal["gemini", "template"]


def template_advice(summary) -> Advice:
    """Lời khuyên mẫu theo nhóm AQI giờ gần nhất (không có thì theo TB 24 giờ)."""
    label = None
    if summary.get("has_data"):
        label = summary.get("aqi_now") or summary.get("aqi_24h")
    headline, text = _TEMPLATES.get(label, _MISSING)
    return Advice(headline, text, "template")


def payload_from_summary(summary) -> dict:
    """Chỉ giữ khoá trong danh sách trắng (cả trong mục forecast)."""
    data = {k: v for k, v in summary.items() if k in ALLOWED_KEYS}
    if isinstance(data.get("forecast"), dict):
        data["forecast"] = {k: v for k, v in data["forecast"].items() if k in FORECAST_KEYS}
    return data


def parse_advice(text) -> Advice | None:
    """Advice nếu text là object JSON có headline, advice là chuỗi khác rỗng, tổng không quá
    MAX_TEXT_CHARS ký tự; ngược lại None."""
    if not isinstance(text, str):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    headline, advice = data.get("headline"), data.get("advice")
    if not isinstance(headline, str) or not isinstance(advice, str):
        return None
    headline, advice = headline.strip(), advice.strip()
    if not headline or not advice or len(headline) + len(advice) > MAX_TEXT_CHARS:
        return None
    return Advice(headline, advice, "gemini")


def _call_with_timeout(fn, timeout_s):
    """Chạy fn() trong luồng nền, chờ tối đa timeout_s giây. Quá hạn thì TimeoutError
    (luồng daemon tự kết thúc sau, không chặn tiến trình thoát)."""
    result = {}

    def target():
        try:
            result["value"] = fn()
        except BaseException as e:      # chuyển lỗi về luồng gọi
            result["error"] = e

    t = threading.Thread(target=target, name="gemini-call", daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError(f"Gemini không trả lời trong {timeout_s} giây")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def get_advice(summary, cfg, client=None) -> Advice:
    """Lời khuyên cho summary. cfg là Config (dùng cfg.gemini, cfg.secrets.gemini_api_key).
    client: đối tượng có .models.generate_content(...) (test truyền client giả); None thì tạo
    google.genai.Client. Không bao giờ raise: lỗi gì cũng trả về lời khuyên mẫu."""
    fallback = template_advice(summary)
    if not summary.get("has_data"):
        return fallback
    g = cfg.gemini
    model = (g.model or "").strip()
    if not model:
        log.info("Chưa đặt gemini.model trong config.yaml: dùng lời khuyên mẫu")
        return fallback
    key = cfg.secrets.gemini_api_key
    try:
        if client is None:
            if not key:
                log.info("Chưa có GEMINI_API_KEY: dùng lời khuyên mẫu")
                return fallback
            from google import genai      # import muộn: chưa cài thì rơi về mẫu
            client = genai.Client(api_key=key,
                                  http_options={"timeout": int(float(g.timeout_s) * 1000)})
        contents = json.dumps(payload_from_summary(summary), ensure_ascii=False)
        config = {
            "system_instruction": SYSTEM_INSTRUCTION,
            "temperature": float(g.temperature),
            "response_mime_type": "application/json",
        }

        def call():
            response = client.models.generate_content(model=model, contents=contents,
                                                      config=config)
            return getattr(response, "text", None)

        text = _call_with_timeout(call, float(g.timeout_s))
    except Exception as e:
        message = str(e)
        if key:
            message = message.replace(key, "***")
        log.warning("Gọi Gemini lỗi (%s: %s): dùng lời khuyên mẫu", type(e).__name__, message[:200])
        return fallback
    parsed = parse_advice(text)
    if parsed is None:
        log.warning("Gemini trả về không đúng JSON {headline, advice} (hoặc quá %d ký tự): "
                    "dùng lời khuyên mẫu", MAX_TEXT_CHARS)
        return fallback
    return parsed
