"""Test đọc cấu hình: giá trị mặc định, chế độ demo, bí mật lấy từ môi trường."""
from pathlib import Path

import pytest

from aiot.config import load_config

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"
SECRET_VARS = ("INGEST_TOKEN", "TELEGRAM_TOKEN")


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
    assert cfg.control.exit_pm25 == 35.0
    assert cfg.control.danger_pm25 == 150.0
    # Nhóm đang bật chế độ demo (commit dc0080f): chỉ thời gian chờ rút ngắn, ngưỡng giữ nguyên
    if cfg.control.demo:
        assert cfg.control.enter_hold_s == cfg.control.demo_wait_s
    assert cfg.cloud.enabled
    assert cfg.cloud.url.startswith("https://")


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


def test_legacy_mqtt_section_does_not_block_cloud(tmp_path, caplog):
    cfg = load_config(*write(tmp_path, "mqtt:\n  host: old-broker\ncloud:\n  enabled: true\n",
                             env_text="INGEST_TOKEN=cloud_token\n"))
    assert cfg.cloud.enabled
    assert cfg.secrets.ingest_token == "cloud_token"
    assert "Mục mqtt đã được gỡ" in caplog.text


def test_forecast_max_age_owned_by_control_only(tmp_path):
    """Tuổi tối đa của dự báo chỉ đặt ở control.forecast_max_age_s (một nơi duy nhất)."""
    cfg = load_config(*write(tmp_path, "control:\n  forecast_max_age_s: 3600\n"))
    assert cfg.control.forecast_max_age_s == 3600
    cfg = load_config(*write(tmp_path, "forecast:\n  obsolete_option: 1\n"))
    assert not hasattr(cfg, "forecast")
    assert cfg.control.forecast_max_age_s == 5400


def test_secrets_come_from_env_not_yaml(tmp_path, monkeypatch):
    cfg = load_config(*write(tmp_path, env_text=(
        "# bình luận\n"
        "INGEST_TOKEN=\"token_dotenv\"\n"
    )))
    assert cfg.secrets.ingest_token == "token_dotenv"

    # Biến môi trường thật thắng giá trị trong .env
    monkeypatch.setenv("INGEST_TOKEN", "tu_moi_truong")
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    assert cfg.secrets.ingest_token == "tu_moi_truong"

    # Biến môi trường rỗng không xoá giá trị trong .env
    monkeypatch.setenv("INGEST_TOKEN", "")
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    assert cfg.secrets.ingest_token == "token_dotenv"


def test_old_telegram_keys_in_env_are_ignored(tmp_path):
    """.env cũ trên Pi còn TELEGRAM_TOKEN, GEMINI_API_KEY, ALLOWED_CHAT_IDS: không lỗi, bỏ qua."""
    cfg = load_config(*write(tmp_path, env_text=(
        "TELEGRAM_TOKEN=abc\nGEMINI_API_KEY=def\nALLOWED_CHAT_IDS=1,x\nINGEST_TOKEN=t\n")))
    assert cfg.secrets.ingest_token == "t"
    assert not hasattr(cfg.secrets, "telegram_token")


def test_secrets_in_yaml_rejected(tmp_path):
    """config.yaml được commit nên không được chứa bí mật."""
    with pytest.raises(ValueError, match=".env"):
        load_config(*write(tmp_path, "secrets:\n  telegram_token: lo_ra_git\n"))


def test_repr_hides_secrets(tmp_path):
    cfg = load_config(*write(tmp_path, env_text=(
        "INGEST_TOKEN=tok789\n"
    )))
    text = repr(cfg) + str(cfg)
    for secret in ("tok789",):
        assert secret not in text


def test_missing_token_does_not_raise(tmp_path):
    # Collector phải chạy được khi chưa có token và chưa có file .env
    cfg = load_config(*write(tmp_path))
    assert cfg.secrets.ingest_token is None


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "khong_co.yaml", tmp_path / ".env")


# ---------------- .env: BOM của Windows và chú thích cuối dòng ----------------
def test_env_with_utf8_bom(tmp_path):
    cfg_path, env_path = write(tmp_path)
    env_path.write_bytes("INGEST_TOKEN=abc\n".encode("utf-8-sig"))
    cfg = load_config(cfg_path, env_path)
    assert cfg.secrets.ingest_token == "abc"


def test_env_inline_comment_stripped_from_unquoted_values(tmp_path):
    from aiot.config import read_env_file

    _, env_path = write(tmp_path, env_text=(
        "A=value # chú thích\n"
        "B=1,2\t# tab rồi chú thích\n"
        "C=\"giữ # trong ngoặc\" # chú thích\n"
        "D=abc#def\n"
        "E='x y'\n"
        "F=\n"))
    values = read_env_file(env_path)
    assert values["A"] == "value"
    assert values["B"] == "1,2"
    assert values["C"] == "giữ # trong ngoặc"
    assert values["D"] == "abc#def"           # không có khoảng trắng trước '#': là một phần giá trị
    assert values["E"] == "x y"
    assert values["F"] == ""


def test_removed_telegram_sections_are_tolerated(tmp_path, caplog):
    """config.yaml cũ trên Pi còn telegram/gemini/report/weather: chỉ cảnh báo, không lỗi."""
    old = ("telegram:\n  quiet_start: 22\ngemini:\n  model: x\nreport:\n  time: '06:30'\n"
           "weather:\n  sunny_lux: 1\n")
    with caplog.at_level("WARNING", logger="aiot.config"):
        cfg = load_config(*write(tmp_path, old))
    for name in ("telegram", "gemini", "report", "weather"):
        assert not hasattr(cfg, name)
        assert name in caplog.text


def test_stale_command_s_in_control(tmp_path):
    cfg = load_config(*write(tmp_path, "control:\n  stale_command_s: 30\n"))
    assert cfg.control.stale_command_s == 30
    assert load_config(REPO_CONFIG, tmp_path / "khong_co.env").control.stale_command_s == 120
