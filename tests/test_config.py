"""Test đọc cấu hình: giá trị mặc định, chế độ demo, bí mật lấy từ môi trường."""
from pathlib import Path

import pytest

from aiot.config import load_config

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"
SECRET_VARS = ("TELEGRAM_TOKEN", "GEMINI_API_KEY", "ALLOWED_CHAT_IDS", "MQTT_PASSWORD")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Xoá biến môi trường thật để test không phụ thuộc máy đang chạy."""
    for name in SECRET_VARS:
        monkeypatch.delenv(name, raising=False)


def write(tmp_path, yaml_text="", env_text=None):
    """Ghi config.yaml (và .env nếu có), trả về (đường dẫn yaml, đường dẫn .env)."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    env_path = tmp_path / ".env"
    if env_text is not None:
        env_path.write_text(env_text, encoding="utf-8")
    return cfg_path, env_path


def test_defaults_match_spec(tmp_path):
    cfg = load_config(*write(tmp_path))
    c = cfg.control
    assert c.enter_pm25 == 55.0
    assert c.enter_mean_s == 600
    assert c.enter_hold_s == 300
    assert c.exit_pm25 == 35.0
    assert c.exit_mean_s == 900
    assert c.exit_hold_s == 900
    assert c.min_state_s == 600
    assert c.danger_pm25 == 150.0
    assert c.stale_s == 120
    assert c.override_ttl_s == 7200
    assert c.demo is False
    assert cfg.db_path == "/home/admin/iot_data/aiot.db"
    assert cfg.csv_path == "/home/admin/iot_data/sensors.csv"
    assert cfg.device_id == "pi01"


def test_repo_config_yaml_loads(tmp_path):
    """config.yaml trong repo phải đọc được và giữ đúng ngưỡng của spec."""
    cfg = load_config(REPO_CONFIG, tmp_path / "khong_co.env")
    assert cfg.control.enter_pm25 == 55.0
    assert cfg.control.demo is False
    assert cfg.mqtt.user == "pi01"


def test_demo_shortens_waits(tmp_path):
    cfg = load_config(*write(tmp_path, "control:\n  demo: true\n  demo_wait_s: 20\n"))
    c = cfg.control
    # Cả thời gian giữ lẫn cửa sổ trung bình đều rút ngắn,
    # nếu không thì demo vẫn phải chờ vài phút
    for name in ("enter_hold_s", "exit_hold_s", "min_state_s", "enter_mean_s", "exit_mean_s"):
        assert getattr(c, name) == 20, name
    assert c.enter_pm25 == 55.0
    assert c.exit_pm25 == 35.0
    assert c.danger_pm25 == 150.0
    assert c.proactive_pm25 == 100.0


def test_yaml_overrides_defaults(tmp_path):
    cfg = load_config(*write(tmp_path, "device_id: pi02\ncontrol:\n  enter_pm25: 60\n"))
    assert cfg.device_id == "pi02"
    assert cfg.control.enter_pm25 == 60
    assert cfg.control.exit_pm25 == 35.0


def test_unknown_key_rejected(tmp_path):
    """Gõ sai tên tham số điều khiển phải báo lỗi, không lặng lẽ dùng mặc định."""
    with pytest.raises(ValueError, match="enter_pm2_5"):
        load_config(*write(tmp_path, "control:\n  enter_pm2_5: 80\n"))


def test_secrets_come_from_env_not_yaml(tmp_path, monkeypatch):
    cfg = load_config(*write(tmp_path, env_text=(
        "# bình luận\n"
        "TELEGRAM_TOKEN=tu_dotenv\n"
        "GEMINI_API_KEY=\"key_trong_ngoac\"\n"
        "MQTT_PASSWORD=mat_khau_dotenv\n"
    )))
    assert cfg.secrets.telegram_token == "tu_dotenv"
    assert cfg.secrets.gemini_api_key == "key_trong_ngoac"
    assert cfg.secrets.mqtt_password == "mat_khau_dotenv"

    # Biến môi trường thật thắng giá trị trong .env
    monkeypatch.setenv("TELEGRAM_TOKEN", "tu_moi_truong")
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    assert cfg.secrets.telegram_token == "tu_moi_truong"

    # Biến môi trường rỗng không xoá giá trị trong .env
    monkeypatch.setenv("MQTT_PASSWORD", "")
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    assert cfg.secrets.mqtt_password == "mat_khau_dotenv"


def test_secrets_in_yaml_rejected(tmp_path):
    """config.yaml được commit nên không được chứa bí mật."""
    with pytest.raises(ValueError, match=".env"):
        load_config(*write(tmp_path, "secrets:\n  telegram_token: lo_ra_git\n"))


def test_repr_hides_secrets(tmp_path):
    cfg = load_config(*write(tmp_path, env_text=(
        "TELEGRAM_TOKEN=abc123\nGEMINI_API_KEY=gem456\nMQTT_PASSWORD=mq789\n"
    )))
    text = repr(cfg) + str(cfg)
    for secret in ("abc123", "gem456", "mq789"):
        assert secret not in text


def test_allowed_chat_ids_parsed(tmp_path):
    cfg = load_config(*write(tmp_path, env_text="ALLOWED_CHAT_IDS=1, 2\n"))
    assert cfg.secrets.allowed_chat_ids == frozenset({1, 2})


def test_allowed_chat_ids_accepts_group_ids(tmp_path):
    cfg = load_config(*write(tmp_path, env_text="ALLOWED_CHAT_IDS=-100123,42,\n"))
    assert cfg.secrets.allowed_chat_ids == frozenset({-100123, 42})


def test_allowed_chat_ids_invalid_items_skipped_with_warning(tmp_path, caplog):
    # ALLOWED_CHAT_IDS sai không được làm hỏng load_config (collector vẫn phải chạy),
    # nhưng phần tử sai không bao giờ được chấp nhận.
    env = "ALLOWED_CHAT_IDS=1,abc, 1_000 ,+5,2.5,-100123,0x10,٣\n"
    with caplog.at_level("WARNING", logger="aiot.config"):
        cfg = load_config(*write(tmp_path, env_text=env))
    assert cfg.secrets.allowed_chat_ids == frozenset({1, -100123})
    assert "ALLOWED_CHAT_IDS" in caplog.text
    assert "abc" not in caplog.text            # không in giá trị (có thể là bí mật dán nhầm)


def test_allowed_chat_ids_all_invalid_gives_empty_set(tmp_path):
    cfg = load_config(*write(tmp_path, env_text="ALLOWED_CHAT_IDS=abc\n"))
    assert cfg.secrets.allowed_chat_ids == frozenset()


def test_missing_telegram_token_does_not_raise(tmp_path):
    # Collector phải chạy được khi chưa có token và chưa có file .env
    cfg = load_config(*write(tmp_path))
    assert cfg.secrets.telegram_token is None
    assert cfg.secrets.gemini_api_key is None
    assert cfg.secrets.mqtt_password is None
    assert cfg.secrets.allowed_chat_ids == frozenset()


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "khong_co.yaml", tmp_path / ".env")
