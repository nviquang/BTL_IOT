"""
Client tối thiểu cho Telegram Bot API (long polling getUpdates, sendMessage) bằng requests.

- Token nằm trong URL (https://api.telegram.org/bot<token>/...), nên mọi lỗi của requests
  đều có thể chứa token. TelegramError luôn được tạo từ thông báo đã che token
  (thay bằng '<token>') và không gắn ngoại lệ gốc (raise ... from None), để traceback
  trong log không lộ URL.
- install_log_redaction() gắn bộ lọc che token vào handler của log; urllib3 phải được
  đặt mức WARNING để không ghi URL từng request (bot.configure_logging làm việc này).
- Tin gửi dạng chữ thường: không có parse_mode, vì nội dung từ Gemini có thể làm hỏng
  Markdown/HTML và Telegram sẽ từ chối cả tin.
"""
import logging
from urllib.parse import quote

import requests

API_URL = "https://api.telegram.org"
MAX_TEXT = 4096                 # giới hạn độ dài một tin của Telegram
HTTP_MARGIN_S = 15              # timeout HTTP = timeout long polling + khoảng này
SEND_TIMEOUT_S = 20


def redact(text, token):
    """Thay token (cả dạng mã hoá URL) trong text bằng '<token>'."""
    text = str(text)
    if not token:
        return text
    for form in {token, quote(token, safe=""), quote(token)}:
        text = text.replace(form, "<token>")
    return text


class TelegramError(Exception):
    """Lỗi gọi Telegram. Thông báo đã che token.

    status: mã HTTP (None khi lỗi mạng); retry_after: số giây phải chờ khi HTTP 429.
    """

    def __init__(self, message, *, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def network(self):
        """Lỗi mạng/timeout (không nhận được phản hồi HTTP)."""
        return self.status is None


class TelegramClient:
    def __init__(self, token, session=None, api_url=API_URL):
        if not token:
            raise ValueError("Thiếu token Telegram")
        self._token = token
        self._base = f"{api_url}/bot{token}"
        self._session = session if session is not None else requests.Session()

    def __repr__(self):
        return "TelegramClient(token=<token>)"

    def _call(self, method, payload, timeout):
        try:
            response = self._session.post(f"{self._base}/{method}", json=payload, timeout=timeout)
        except requests.RequestException as e:
            raise TelegramError(
                f"{method}: lỗi mạng {type(e).__name__}: {redact(e, self._token)}") from None
        try:
            data = response.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {response.status_code}, phản hồi không phải JSON",
                                status=response.status_code) from None
        if not isinstance(data, dict):
            raise TelegramError(f"{method}: phản hồi không hợp lệ", status=response.status_code)
        if response.status_code != 200 or not data.get("ok"):
            params = data.get("parameters") or {}
            retry_after = params.get("retry_after") if isinstance(params, dict) else None
            description = redact(data.get("description", ""), self._token)
            raise TelegramError(
                f"{method}: HTTP {response.status_code} {description}".rstrip(),
                status=data.get("error_code", response.status_code),
                retry_after=retry_after if isinstance(retry_after, int) else None)
        return data.get("result")

    def get_updates(self, offset, timeout):
        """Long polling: chờ tối đa timeout giây. offset None: lấy mọi update còn giữ."""
        payload = {"timeout": int(timeout), "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = int(offset)
        result = self._call("getUpdates", payload, timeout=int(timeout) + HTTP_MARGIN_S)
        return result if isinstance(result, list) else []

    def send(self, chat_id, text):
        """Gửi tin chữ thường (không parse_mode). Tin quá dài bị cắt còn MAX_TEXT ký tự."""
        text = str(text)
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT - 1] + "…"
        self._call("sendMessage",
                   {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                   timeout=SEND_TIMEOUT_S)


class RedactFilter(logging.Filter):
    """Che token trong thông điệp log và traceback trước khi handler ghi ra."""

    def __init__(self, token):
        super().__init__()
        self._token = token

    def filter(self, record):
        message = record.getMessage()
        clean = redact(message, self._token)
        if clean != message:
            record.msg, record.args = clean, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self._token)
        if record.stack_info:
            record.stack_info = redact(record.stack_info, self._token)
        return True


def install_log_redaction(token, handlers=None):
    """Gắn RedactFilter vào handlers (mặc định mọi handler của root logger)."""
    if handlers is None:
        handlers = logging.getLogger().handlers
    for handler in handlers:
        handler.addFilter(RedactFilter(token))
