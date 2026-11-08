"""
Client tối thiểu cho Telegram Bot API (getUpdates, sendMessage, sendPhoto, setMyCommands,
deleteMessages) bằng requests.

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
import os
from urllib.parse import quote

import requests

log = logging.getLogger("aiot.telegram_api")

API_URL = "https://api.telegram.org"
MAX_TEXT = 4096                 # giới hạn độ dài một tin của Telegram
MAX_CAPTION = 1024              # giới hạn chú thích ảnh
HTTP_MARGIN_S = 15              # timeout HTTP = timeout long polling + khoảng này
SEND_TIMEOUT_S = 20
DELETE_BATCH = 100              # deleteMessages nhận 1–100 message_id mỗi lần
PHOTO_TIMEOUT_S = 60            # tải ảnh lên chậm hơn khi mạng yếu


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

    def _call(self, method, payload, timeout, files=None):
        """files None: gửi JSON; có files: gửi multipart (payload là các trường form)."""
        try:
            if files is None:
                response = self._session.post(f"{self._base}/{method}", json=payload,
                                              timeout=timeout)
            else:
                response = self._session.post(f"{self._base}/{method}", data=payload,
                                              files=files, timeout=timeout)
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
        """Gửi tin chữ thường (không parse_mode). Tin quá dài bị cắt còn MAX_TEXT ký tự.
        Trả về message_id của tin đã gửi (None nếu phản hồi không có)."""
        text = str(text)
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT - 1] + "…"
        return _message_id(self._call(
            "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=SEND_TIMEOUT_S))

    def send_photo(self, chat_id, path, caption):
        """Gửi ảnh JPEG (multipart sendPhoto) kèm chú thích chữ thường (không parse_mode),
        cắt còn MAX_CAPTION ký tự. Lỗi Telegram/mạng: TelegramError như send(); không mở
        được file: OSError (người gọi tự gửi chữ thay thế). Trả về message_id như send()."""
        caption = str(caption)
        if len(caption) > MAX_CAPTION:
            caption = caption[:MAX_CAPTION - 1] + "…"
        with open(path, "rb") as f:
            return _message_id(self._call(
                "sendPhoto", {"chat_id": str(chat_id), "caption": caption},
                timeout=PHOTO_TIMEOUT_S,
                files={"photo": (os.path.basename(path), f, "image/jpeg")}))

    def delete_messages(self, chat_id, message_ids):
        """Xoá tin bằng deleteMessages, mỗi lần tối đa DELETE_BATCH id theo đúng thứ tự
        truyền vào. Telegram bỏ qua id không tìm thấy; chỉ xoá được tin gửi < 48 giờ.
        Một lần gọi lỗi (TelegramError, đã che token) chỉ ghi cảnh báo, lần sau vẫn chạy.
        Trả về số lần gọi thành công."""
        ids = [int(m) for m in message_ids]
        done = 0
        for i in range(0, len(ids), DELETE_BATCH):
            chunk = ids[i:i + DELETE_BATCH]
            try:
                self._call("deleteMessages", {"chat_id": chat_id, "message_ids": chunk},
                           timeout=SEND_TIMEOUT_S)
                done += 1
            except TelegramError as e:
                log.warning("Không xoá được %d tin (id %s..%s): %s",
                            len(chunk), chunk[0], chunk[-1], e)
        return done

    def get_me(self, timeout=SEND_TIMEOUT_S):
        """Thông tin bot (kiểm tra token). Lỗi: TelegramError đã che token."""
        return self._call("getMe", {}, timeout=timeout)

    def set_my_commands(self, commands):
        """Đặt menu lệnh của bot. commands: [{"command": "now", "description": "..."}]."""
        self._call("setMyCommands",
                   {"commands": [{"command": c["command"], "description": c["description"]}
                                 for c in commands]},
                   timeout=SEND_TIMEOUT_S)


def _message_id(result):
    """message_id trong kết quả sendMessage/sendPhoto, None nếu không có."""
    if isinstance(result, dict):
        mid = result.get("message_id")
        if isinstance(mid, int) and not isinstance(mid, bool):
            return mid
    return None


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
