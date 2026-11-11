"""
Đọc cấu hình chung cho mọi dịch vụ của trạm.

- Tham số thường nằm trong config.yaml (được commit).
- Bí mật (token, API key, mật khẩu MQTT, danh sách chat) chỉ nằm trong .env
  hoặc biến môi trường; biến môi trường thật thắng giá trị trong .env.
- Thiếu bí mật không làm lỗi: collector vẫn phải chạy khi chưa có token.
"""
import dataclasses
import logging
import os
import re
from dataclasses import dataclass, field

import yaml

log = logging.getLogger("aiot.config")


@dataclass(frozen=True)
class MqttCfg:
    host: str = "localhost"         # IP laptop chạy Mosquitto
    port: int = 1883
    user: str = "pi01"
    enabled: bool = True


@dataclass(frozen=True)
class ControlCfg:
    """Luật bật/tắt quạt (xem 'Logic điều khiển quạt' trong CLAUDE.md)."""
    enter_pm25: float = 55.0        # vào BẢO VỆ khi TB enter_mean_s vượt ngưỡng này...
    enter_mean_s: int = 600
    enter_hold_s: int = 300         # ...liên tục ít nhất enter_hold_s
    exit_pm25: float = 35.0         # thoát khi TB exit_mean_s dưới ngưỡng này...
    exit_mean_s: int = 900
    exit_hold_s: int = 900          # ...liên tục ít nhất exit_hold_s
    min_state_s: int = 600          # và đã giữ trạng thái ít nhất min_state_s
    danger_pm25: float = 150.0      # mức khẩn cấp: vào ngay
    danger_window_s: int = 60
    proactive_pm25: float = 100.0   # dự báo TB 3 giờ tới vượt ngưỡng này thì bật trước
    forecast_max_age_s: int = 5400  # dự báo cũ hơn thì bỏ qua
    stale_s: int = 120              # mất dữ liệu PM lâu hơn thì giữ nguyên trạng thái
    override_ttl_s: int = 7200      # ghi đè thủ công hết hạn sau 2 giờ
    loop_s: int = 20
    demo: bool = False              # rút ngắn thời gian chờ để trình diễn
    demo_wait_s: int = 20


@dataclass(frozen=True)
class TelegramCfg:
    quiet_start: int = 22           # giờ yên lặng 22h -> 6h
    quiet_end: int = 6
    alert_cooldown_s: int = 1800    # tối đa 1 tin cùng loại mỗi 30 phút
    stale_command_s: int = 120      # bỏ lệnh điều khiển gửi quá lâu (sau khi mất mạng)
    poll_timeout_s: int = 50


@dataclass(frozen=True)
class GeminiCfg:
    model: str = ""                 # điền sau khi kiểm tra model và hạn mức hiện hành
    temperature: float = 0.2
    max_calls_per_day: int = 3
    timeout_s: int = 20


@dataclass(frozen=True)
class ForecastCfg:
    model_path: str = "/home/admin/BTL_IOT/models/pm25_lstm.tflite"
    scaler_path: str = "/home/admin/BTL_IOT/models/scaler.json"
    # Tuổi tối đa của dự báo chỉ đặt ở control.forecast_max_age_s


@dataclass(frozen=True)
class ReportCfg:
    time: str = "06:30"             # giờ gửi báo cáo sáng


@dataclass(frozen=True)
class WeatherCfg:
    """Cảnh báo nắng gắt / có thể mưa từ cảm biến ngoài ban công (aiot/weather.py)."""
    sunny_lux: float = 30000.0      # nắng gắt: lux > sunny_lux VÀ nhiệt độ >= sunny_temp_c
    sunny_temp_c: float = 33.0
    rain_hum_pct: float = 85.0      # có thể mưa: độ ẩm >= rain_hum_pct VÀ lux < rain_lux,
    rain_lux: float = 5000.0        # chỉ xét trong [day_start_h, day_end_h) giờ địa phương;
    day_start_h: int = 8            # tránh bình minh (~06:10) và hoàng hôn (~17:15) ở Hà Nội
    day_end_h: int = 16             # tháng 11: lux thấp + ẩm lúc đó không phải sắp mưa
    sustain_windows: int = 3        # số cửa sổ 5 phút liền nhau phải thoả; 0 = tức thời (mẫu thô)
    cooldown_s: int = 10800         # tối đa 1 tin cùng loại mỗi 3 giờ
    photo_dir: str = "/home/admin/iot_data/photos"
    photo_keep_days: int = 7
    camera_index: int = 0           # /dev/video0


@dataclass(frozen=True)
class Secrets:
    telegram_token: str | None = None
    gemini_api_key: str | None = None
    allowed_chat_ids: frozenset[int] = frozenset()
    mqtt_password: str | None = None

    def __repr__(self):
        # Chỉ cho biết đã có hay chưa, không bao giờ in giá trị
        def state(value):
            return "<đã đặt>" if value else None
        return (f"Secrets(telegram_token={state(self.telegram_token)}, "
                f"gemini_api_key={state(self.gemini_api_key)}, "
                f"allowed_chat_ids={len(self.allowed_chat_ids)} chat, "
                f"mqtt_password={state(self.mqtt_password)})")

    __str__ = __repr__


@dataclass(frozen=True)
class Config:
    db_path: str = "/home/admin/iot_data/aiot.db"
    csv_path: str = "/home/admin/iot_data/sensors.csv"
    device_id: str = "pi01"
    mqtt: MqttCfg = field(default_factory=MqttCfg)
    control: ControlCfg = field(default_factory=ControlCfg)
    telegram: TelegramCfg = field(default_factory=TelegramCfg)
    gemini: GeminiCfg = field(default_factory=GeminiCfg)
    forecast: ForecastCfg = field(default_factory=ForecastCfg)
    report: ReportCfg = field(default_factory=ReportCfg)
    weather: WeatherCfg = field(default_factory=WeatherCfg)
    secrets: Secrets = field(default_factory=Secrets)


# Mục con trong config.yaml -> lớp dataclass tương ứng
SECTIONS = {
    "mqtt": MqttCfg,
    "control": ControlCfg,
    "telegram": TelegramCfg,
    "gemini": GeminiCfg,
    "forecast": ForecastCfg,
    "report": ReportCfg,
    "weather": WeatherCfg,
}
TOP_LEVEL_KEYS = ("db_path", "csv_path", "device_id")


def read_env_file(path):
    """Đọc file .env dạng KEY=VALUE. Không có file thì trả về dict rỗng.

    - utf-8-sig: bỏ BOM đầu file khi .env được lưu bằng Notepad trên Windows.
    - Giá trị trong ngoặc kép/đơn: lấy phần trong ngoặc, bỏ mọi thứ sau dấu đóng ngoặc.
    - Giá trị không có ngoặc: bỏ chú thích cuối dòng dạng " # ..." (có khoảng trắng trước
      '#'); 'abc#def' giữ nguyên.
    """
    values = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] in "\"'" and value.find(value[0], 1) != -1:
            value = value[1:value.find(value[0], 1)]
        else:
            value = _INLINE_COMMENT_RE.split(value, maxsplit=1)[0].strip()
        values[key] = value
    return values


_CHAT_ID_RE = re.compile(r"-?[0-9]+", re.ASCII)
_INLINE_COMMENT_RE = re.compile(r"\s+#")


def parse_chat_ids(text):
    """'1, 2' -> frozenset({1, 2}). Bỏ phần tử rỗng, ID nhóm có thể âm.

    Không bao giờ raise: phần tử không phải số nguyên thập phân (ví dụ 'abc', '1_000',
    '+5', '2.5') bị bỏ kèm cảnh báo, để một dòng .env gõ sai không làm hỏng mọi dịch vụ.
    Cảnh báo chỉ nêu vị trí, không in giá trị (có thể là bí mật dán nhầm chỗ).
    Bot tự từ chối chạy khi danh sách rỗng.
    """
    ids = set()
    for position, part in enumerate((text or "").split(","), start=1):
        part = part.strip()
        if not part:
            continue
        if _CHAT_ID_RE.fullmatch(part):
            ids.add(int(part))
        else:
            log.warning("ALLOWED_CHAT_IDS: phần tử thứ %d không phải số nguyên, bỏ qua", position)
    return frozenset(ids)


def load_secrets(env_path):
    """Gộp .env với biến môi trường (biến môi trường khác rỗng thắng)."""
    dotenv = read_env_file(env_path)

    def get(name):
        # chuỗi rỗng coi như chưa đặt
        return os.environ.get(name) or dotenv.get(name) or None

    return Secrets(
        telegram_token=get("TELEGRAM_TOKEN"),
        gemini_api_key=get("GEMINI_API_KEY"),
        allowed_chat_ids=parse_chat_ids(get("ALLOWED_CHAT_IDS")),
        mqtt_password=get("MQTT_PASSWORD"),
    )


def build_section(cls, name, data):
    """Tạo dataclass của một mục, báo lỗi nếu có khoá lạ (thường là gõ sai tên)."""
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ValueError(f"Mục '{name}' trong config.yaml phải là một bảng khoá: giá trị")
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"Khoá không hợp lệ trong mục '{name}': {', '.join(unknown)}")
    return cls(**data)


def apply_demo(control):
    """Chế độ demo: rút ngắn mọi thời gian chờ và cửa sổ trung bình, giữ nguyên ngưỡng PM."""
    if not control.demo:
        return control
    wait = control.demo_wait_s
    return dataclasses.replace(
        control,
        enter_mean_s=wait,
        enter_hold_s=wait,
        exit_mean_s=wait,
        exit_hold_s=wait,
        min_state_s=wait,
    )


def load_config(path="config.yaml", env_path=".env"):
    """Đọc config.yaml và bí mật từ .env/biến môi trường, trả về Config."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml phải là một bảng khoá: giá trị")
    if "secrets" in data:
        raise ValueError("Không đặt bí mật trong config.yaml; dùng file .env hoặc biến môi trường")

    unknown = sorted(set(data) - set(SECTIONS) - set(TOP_LEVEL_KEYS))
    if unknown:
        raise ValueError(f"Khoá không hợp lệ trong config.yaml: {', '.join(unknown)}")

    sections = {name: build_section(cls, name, data.get(name)) for name, cls in SECTIONS.items()}
    sections["control"] = apply_demo(sections["control"])
    top = {key: data[key] for key in TOP_LEVEL_KEYS if key in data}
    return Config(**top, **sections, secrets=load_secrets(env_path))
