"""
Đọc cấu hình chung cho mọi dịch vụ của trạm.

- Tham số thường nằm trong config.yaml (được commit).
- Bí mật (INGEST_TOKEN của web) chỉ nằm trong .env hoặc biến môi trường; biến môi
  trường thật thắng giá trị trong .env.
- Thiếu bí mật không làm lỗi: collector và controller vẫn phải chạy khi chưa có token.
"""
import dataclasses
import logging
import os
import re
from dataclasses import dataclass, field

import yaml

log = logging.getLogger("aiot.config")


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
    stale_command_s: int = 120      # bỏ lệnh điều khiển (web) chờ quá lâu, ví dụ controller từng dừng
    loop_s: int = 20
    demo: bool = False              # rút ngắn thời gian chờ để trình diễn
    demo_wait_s: int = 20


@dataclass(frozen=True)
class BuzzerCfg:
    """Còi báo vượt ngưỡng tại chỗ (aiot/alarm.py, aiot/buzzer.py), chạy trong controller."""
    enabled: bool = True
    gpio: int = 5                   # GPIO5 = pin 29, mặc định kéo lên khi Pi khởi động
    active_low: bool = True         # module ghi 低电平触发: LOW = kêu
    alarm_pm25: float = 150.0       # kêu khi trung vị PM2.5 1 phút >= ngưỡng này
    beep_on_s: float = 0.3          # mỗi tiếng bíp
    beep_off_s: float = 0.2
    burst_s: float = 10.0           # một hồi còi dài bao lâu
    repeat_s: int = 300             # còn vượt ngưỡng thì kêu lại sau chừng này giây
    mute_s: int = 1800              # lệnh tắt tiếng không kèm thời gian thì tắt chừng này giây
    manual_max_s: int = 60          # bật còi thủ công từ web tối đa chừng này giây


@dataclass(frozen=True)
class SourceCfg:
    """Bộ nhận diện nguồn bụi (aiot/source_model.py), chạy trong controller mỗi control.loop_s.

    enabled và mô hình nạp được: quạt và còi theo nhãn AI (nấu ăn: quạt; ô nhiễm bụi mịn:
    quạt + còi). Không có mô hình, thiếu dữ liệu 1 giây hay chưa đủ tin
    cậy: quay về luật ngưỡng PM2.5 (control, buzzer.alarm_pm25)."""
    enabled: bool = True
    model_path: str = "/home/admin/BTL_IOT/models/source_logreg.json"
    vote_n: int = 3                 # trung bình xác suất của 3 lần dự đoán gần nhất (15 giây)
    min_confidence: float = 0.6     # chỉ đổi nhãn khi xác suất trung bình >= ngưỡng này
    max_gap_s: int = 30             # dự đoán cách nhau lâu hơn thì làm mượt lại từ đầu
    stale_s: int = 15               # mẫu 1 giây mới nhất cũ hơn: không dự đoán


@dataclass(frozen=True)
class CloudCfg:
    """Đẩy dữ liệu lên web trên cloud và nhận lệnh từ web (aiot/uplink.py, thư mục cloud/)."""
    enabled: bool = False
    url: str = ""                   # ví dụ https://aiot-tram.onrender.com (không có / cuối)
    interval_s: int = 5             # mỗi lần gửi /api/ingest
    timeout_s: int = 10
    history_step_s: int = 60        # history gửi lên là trung bình theo bước này
    command_max_age_s: int = 60     # lệnh web cũ hơn thì bỏ


@dataclass(frozen=True)
class Secrets:
    ingest_token: str | None = None

    def __repr__(self):
        # Chỉ cho biết đã có hay chưa, không bao giờ in giá trị
        return f"Secrets(ingest_token={'<đã đặt>' if self.ingest_token else None})"

    __str__ = __repr__


@dataclass(frozen=True)
class Config:
    db_path: str = "/home/admin/iot_data/aiot.db"
    csv_path: str = "/home/admin/iot_data/sensors.csv"
    device_id: str = "pi01"
    control: ControlCfg = field(default_factory=ControlCfg)
    buzzer: BuzzerCfg = field(default_factory=BuzzerCfg)
    source: SourceCfg = field(default_factory=SourceCfg)
    cloud: CloudCfg = field(default_factory=CloudCfg)
    secrets: Secrets = field(default_factory=Secrets)


# Mục con trong config.yaml -> lớp dataclass tương ứng
SECTIONS = {
    "control": ControlCfg,
    "buzzer": BuzzerCfg,
    "source": SourceCfg,
    "cloud": CloudCfg,
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


_INLINE_COMMENT_RE = re.compile(r"\s+#")


def load_secrets(env_path):
    """Gộp .env với biến môi trường (biến môi trường khác rỗng thắng)."""
    dotenv = read_env_file(env_path)

    def get(name):
        # chuỗi rỗng coi như chưa đặt
        return os.environ.get(name) or dotenv.get(name) or None

    return Secrets(ingest_token=get("INGEST_TOKEN"))


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

    # Cho phép cập nhật Pi mà vẫn dùng config.yaml từ phiên bản cũ (Telegram, Gemini, báo cáo
    # sáng và cảnh báo nắng/mưa đã gỡ ngày 29/11/2026).
    for section in ("mqtt", "forecast", "telegram", "gemini", "report", "weather"):
        if section in data:
            del data[section]
            log.warning("Mục %s đã được gỡ, có thể xoá khỏi config.yaml", section)

    unknown = sorted(set(data) - set(SECTIONS) - set(TOP_LEVEL_KEYS))
    if unknown:
        raise ValueError(f"Khoá không hợp lệ trong config.yaml: {', '.join(unknown)}")

    sections = {name: build_section(cls, name, data.get(name)) for name, cls in SECTIONS.items()}
    sections["control"] = apply_demo(sections["control"])
    top = {key: data[key] for key in TOP_LEVEL_KEYS if key in data}
    return Config(**top, **sections, secrets=load_secrets(env_path))
