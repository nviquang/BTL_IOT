#!/usr/bin/env bash
# Bật toàn bộ trạm AIoT trên Raspberry Pi bằng một lệnh (chạy trên Pi, user admin).
#   bash ~/BTL_IOT/scripts/pi_up.sh            cài/cập nhật rồi bật mọi dịch vụ
#   bash ~/BTL_IOT/scripts/pi_up.sh --no-fan   như trên nhưng không chạy aiot-controller
#   bash ~/BTL_IOT/scripts/pi_up.sh --stop     dừng mọi dịch vụ và timer (quạt tắt)
#   bash ~/BTL_IOT/scripts/pi_up.sh --status   chỉ in bảng kiểm tra (không cần sudo)
# Chạy lại bao nhiêu lần cũng được: bước nào đã xong thì bỏ qua, dịch vụ được khởi động
# lại để nạp code mới. Không bao giờ in giá trị trong .env.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

ENV_KEYS=(TELEGRAM_TOKEN GEMINI_API_KEY ALLOWED_CHAT_IDS MQTT_PASSWORD)
# Mỗi mục: các tên gói thay thế nhau (tên khác nhau giữa các bản Raspberry Pi OS)
APT_PACKAGES=("libgpiod2|libgpiod3" "sysv-ipc|python3-sysv-ipc" "i2c-tools" "python3-venv" "sqlite3")
GROUPS_NEEDED=(gpio i2c dialout video)
TIMEZONE="Asia/Ho_Chi_Minh"
SERVICES=(aiot-collector aiot-controller aiot-bot)
TIMERS=(aiot-forecast.timer aiot-morning.timer aiot-weather.timer)
VENV_PY="$REPO_DIR/venv/bin/python"
REQ_STAMP="$REPO_DIR/venv/.requirements-pi.sha256"
DATA_DIR="/home/admin/iot_data"
WAIT_S=30

if [[ -t 1 ]]; then
  C_BOLD=$'\e[1m'; C_GREEN=$'\e[32m'; C_YELLOW=$'\e[33m'; C_RED=$'\e[31m'; C_RESET=$'\e[0m'
else
  C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi

section() { printf '\n%s==> %s%s\n' "$C_BOLD" "$1" "$C_RESET"; }
info()    { printf '    %s\n' "$1"; }
ok()      { printf '    %s✔%s %s\n' "$C_GREEN" "$C_RESET" "$1"; }
warn()    { printf '    %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$1"; }
die()     { printf '%sLỗi:%s %s\n' "$C_RED" "$C_RESET" "$1" >&2; exit 1; }

usage() {
  cat <<'EOF'
Cách dùng: bash scripts/pi_up.sh [--no-fan | --stop | --status | --help]

  (không cờ)  cài phần còn thiếu, cập nhật unit systemd, bật và khởi động lại dịch vụ,
              rồi in bảng kiểm tra sức khoẻ
  --no-fan    như trên nhưng dừng và tắt tự khởi động aiot-controller (quạt không bị điều khiển)
  --stop      dừng mọi dịch vụ và timer (controller dừng thì quạt tắt); không gỡ tự khởi động
  --status    chỉ in bảng kiểm tra sức khoẻ, không cần sudo
  --help      in hướng dẫn này
EOF
}

MODE="up"
NO_FAN=0
for arg in "$@"; do
  case "$arg" in
    --no-fan) NO_FAN=1 ;;
    --stop) MODE="stop" ;;
    --status) MODE="status" ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "không hiểu tuỳ chọn '$arg'" ;;
  esac
done

# ---------------- tiện ích ----------------
# Khoá có giá trị khác rỗng trong .env không (không in giá trị). Cùng quy tắc với
# aiot.config.read_env_file: bỏ "export ", ngoặc kép/đơn, chú thích " #...", CR của Windows.
env_has() {
  local key="$1" line value
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" .env | tail -n 1 || true)"
  [[ -n "$line" ]] || return 1
  value="${line#*=}"
  value="${value//$'\r'/}"
  value="${value#"${value%%[![:space:]]*}"}"
  if [[ "$value" == \"* || "$value" == \'* ]]; then
    local quote="${value:0:1}"
    value="${value:1}"
    value="${value%%"$quote"*}"
  else
    value="$(printf '%s' "$value" | sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]+$//')"
  fi
  [[ -n "$value" ]]
}

pkg_installed() { dpkg -s "$1" >/dev/null 2>&1; }

unit_enabled() { [[ "$(systemctl is-enabled "$1" 2>/dev/null || true)" == "enabled" ]]; }

run_healthcheck() {
  local args=()
  if ! unit_enabled aiot-controller; then
    args+=(--skip-unit aiot-controller)
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    die "chưa có venv ($VENV_PY); chạy: bash scripts/pi_up.sh"
  fi
  section "Bảng kiểm tra sức khoẻ"
  "$VENV_PY" -m aiot.healthcheck "${args[@]}"
}

# ---------------- --status ----------------
if [[ "$MODE" == "status" ]]; then
  set +e
  run_healthcheck
  exit $?
fi

# ---------------- 1. kiểm tra trước ----------------
section "Kiểm tra trước khi chạy"
[[ "$(id -un)" == "admin" ]] || die "phải chạy bằng user admin (đang là $(id -un))"
[[ "$REPO_DIR" == "/home/admin/BTL_IOT" ]] \
  || warn "repo đang ở $REPO_DIR; các unit systemd dùng /home/admin/BTL_IOT"
ok "user admin, thư mục $REPO_DIR"

BOT_ENABLED=1
if [[ "$MODE" == "up" ]]; then
  [[ -f .env ]] || die "không có file $REPO_DIR/.env. Chép .env.example thành .env, điền 4 khoá (README mục Chạy nhanh) rồi chạy lại."
  chmod 600 .env
  missing=()
  for key in "${ENV_KEYS[@]}"; do
    env_has "$key" || missing+=("$key")
  done
  if (( ${#missing[@]} == 0 )); then
    ok ".env đủ 4 khoá (đã chmod 600)"
  else
    warn ".env thiếu hoặc rỗng: ${missing[*]}"
  fi
  if ! env_has TELEGRAM_TOKEN || ! env_has ALLOWED_CHAT_IDS; then
    BOT_ENABLED=0
    warn "thiếu TELEGRAM_TOKEN hoặc ALLOWED_CHAT_IDS: bỏ qua aiot-bot (không có tin Telegram)"
  fi
fi

# ---------------- 2. sudo một lần ----------------
section "Quyền sudo"
info "Nhập mật khẩu của user admin nếu được hỏi (chỉ một lần)."
sudo -v || die "không lấy được quyền sudo"
( while true; do sleep 50; kill -0 "$$" 2>/dev/null || exit 0; sudo -n true 2>/dev/null || exit 0; done ) &
SUDO_KEEPALIVE_PID=$!
trap 'kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true' EXIT
ok "đã có quyền sudo"

# ---------------- --stop ----------------
if [[ "$MODE" == "stop" ]]; then
  section "Dừng dịch vụ"
  sudo systemctl stop "${TIMERS[@]}" "${SERVICES[@]}" || warn "có unit không dừng được"
  ok "đã dừng ${TIMERS[*]} ${SERVICES[*]}"
  info "aiot-controller dừng thì đưa GPIO23 về LOW: quạt tắt."
  info "Tự khởi động khi bật Pi vẫn giữ nguyên; bật lại: bash scripts/pi_up.sh"
  exit 0
fi

# ---------------- 3. cài đặt lần đầu ----------------
REBOOT_NEEDED=0

section "Gói hệ thống"
to_install=()
apt_updated=0
for entry in "${APT_PACKAGES[@]}"; do
  IFS='|' read -r -a candidates <<< "$entry"
  have=0
  for pkg in "${candidates[@]}"; do
    if pkg_installed "$pkg"; then have=1; break; fi
  done
  (( have )) && continue
  if (( ! apt_updated )); then
    info "Cập nhật danh sách gói (apt-get update)..."
    sudo apt-get update -qq || warn "apt-get update lỗi (mất mạng?)"
    apt_updated=1
  fi
  chosen=""
  for pkg in "${candidates[@]}"; do
    if apt-cache show "$pkg" >/dev/null 2>&1; then chosen="$pkg"; break; fi
  done
  if [[ -n "$chosen" ]]; then
    to_install+=("$chosen")
  else
    warn "không tìm thấy gói nào trong: ${candidates[*]} (bỏ qua)"
  fi
done
if (( ${#to_install[@]} > 0 )); then
  info "Cài: ${to_install[*]}"
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${to_install[@]}" \
    || warn "cài gói lỗi; DHT11 có thể đọc lỗi 'Checksum did not validate'"
else
  ok "đã có đủ gói hệ thống"
fi

section "I2C, UART, múi giờ, nhóm quyền"
if command -v raspi-config >/dev/null 2>&1; then
  if [[ "$(sudo raspi-config nonint get_i2c)" != "0" ]]; then
    sudo raspi-config nonint do_i2c 0; REBOOT_NEEDED=1; ok "đã bật I2C (BH1750)"
  else
    ok "I2C đã bật"
  fi
  if [[ "$(sudo raspi-config nonint get_serial_hw)" != "0" ]]; then
    sudo raspi-config nonint do_serial_hw 0; REBOOT_NEEDED=1; ok "đã bật UART phần cứng (PMS7003)"
  else
    ok "UART phần cứng đã bật"
  fi
  if [[ "$(sudo raspi-config nonint get_serial_cons)" != "1" ]]; then
    sudo raspi-config nonint do_serial_cons 1; REBOOT_NEEDED=1; ok "đã tắt serial console trên UART"
  else
    ok "serial console đã tắt"
  fi
else
  warn "không có raspi-config: tự kiểm tra I2C và UART"
fi

if [[ "$(timedatectl show -p Timezone --value 2>/dev/null || true)" != "$TIMEZONE" ]]; then
  sudo timedatectl set-timezone "$TIMEZONE"
  ok "đã đặt múi giờ $TIMEZONE"
else
  ok "múi giờ $TIMEZONE"
fi

current_groups=" $(id -nG admin) "
add_groups=()
for g in "${GROUPS_NEEDED[@]}"; do
  [[ "$current_groups" == *" $g "* ]] && continue
  getent group "$g" >/dev/null || { warn "không có nhóm $g"; continue; }
  add_groups+=("$g")
done
if (( ${#add_groups[@]} > 0 )); then
  sudo usermod -aG "$(IFS=,; echo "${add_groups[*]}")" admin
  ok "đã thêm admin vào nhóm: ${add_groups[*]} (dịch vụ dùng ngay; phiên SSH cần đăng nhập lại)"
else
  ok "admin đã thuộc nhóm ${GROUPS_NEEDED[*]}"
fi

section "Python venv và thư viện"
if [[ ! -x "$VENV_PY" ]]; then
  info "Tạo venv..."
  python3 -m venv venv
  "$VENV_PY" -m pip install --upgrade pip -q
  ok "đã tạo venv"
fi

lgpio_wheel_url() {
  local tag
  tag="$("$VENV_PY" -c 'import sys; print(f"cp{sys.version_info[0]}{sys.version_info[1]}")')"
  if [[ "$tag" != "cp313" && "$tag" != "cp314" ]]; then
    tag="cp313"
  fi
  printf 'https://github.com/adafruit/lgpio-python-wheels/raw/main/wheels/lgpio-0.2.2.0-%s-%s-linux_aarch64.whl' "$tag" "$tag"
}

install_lgpio_wheel() {
  info "Cài lgpio từ wheel của Adafruit (PyPI không có wheel cho Python này)..."
  "$VENV_PY" -m pip install -q --force-reinstall "$(lgpio_wheel_url)"
}

req_hash="$(sha256sum requirements-pi.txt | cut -d' ' -f1)"
if [[ -f "$REQ_STAMP" && "$(cat "$REQ_STAMP")" == "$req_hash" ]]; then
  ok "thư viện khớp requirements-pi.txt (bỏ qua pip)"
else
  info "Cài requirements-pi.txt (lần đầu có thể mất 10–30 phút vì ultralytics/torch)..."
  pip_ok=1
  if ! "$VENV_PY" -m pip install -q -r requirements-pi.txt; then
    warn "pip lỗi, thường do lgpio phải build từ source; thử wheel của Adafruit rồi cài lại"
    if install_lgpio_wheel && "$VENV_PY" -m pip install -q -r requirements-pi.txt; then
      ok "cài lại thành công"
    else
      pip_ok=0
    fi
  fi
  if ! "$VENV_PY" -c 'import lgpio' >/dev/null 2>&1; then
    install_lgpio_wheel || pip_ok=0
    "$VENV_PY" -c 'import lgpio' >/dev/null 2>&1 || { warn "import lgpio vẫn lỗi (README chi tiết, Phụ lục C)"; pip_ok=0; }
  fi
  if ! "$VENV_PY" -c 'import sysv_ipc' >/dev/null 2>&1; then
    "$VENV_PY" -m pip install -q sysv_ipc || warn "không cài được sysv_ipc (DHT11 cần)"
  fi
  if (( pip_ok )); then
    printf '%s\n' "$req_hash" > "$REQ_STAMP"
    ok "đã cài thư viện"
  else
    warn "thư viện chưa cài đủ; lần chạy sau sẽ thử lại"
  fi
fi

mkdir -p "$DATA_DIR/photos"
ok "thư mục dữ liệu $DATA_DIR"

# ---------------- 4. client cũ ----------------
section "Client cũ"
if pgrep -f 'pi_sensor_client\.py' >/dev/null 2>&1; then
  sudo pkill -f 'pi_sensor_client\.py' || true
  ok "đã dừng pi_sensor_client.py (dùng chung cảm biến với aiot-collector)"
else
  ok "pi_sensor_client.py không chạy"
fi
if pgrep -f 'pi_(camera|detection)_client\.py' >/dev/null 2>&1; then
  warn "pi_camera_client.py/pi_detection_client.py đang chạy và giữ camera: /photo và ảnh cảnh báo sẽ không chụp được"
fi

# ---------------- 5. unit systemd ----------------
section "Dịch vụ systemd"
units_changed=0
for src in deploy/aiot-*.service deploy/aiot-*.timer; do
  dest="/etc/systemd/system/$(basename "$src")"
  if ! cmp -s "$src" "$dest"; then
    sudo install -m 644 "$src" "$dest"
    units_changed=1
    info "cập nhật $(basename "$src")"
  fi
done
if (( units_changed )); then
  sudo systemctl daemon-reload
  ok "đã nạp lại unit (daemon-reload)"
else
  ok "unit đã mới nhất"
fi

wanted=(aiot-collector)
skipped=()
if (( NO_FAN )); then skipped+=(aiot-controller); else wanted+=(aiot-controller); fi
if (( BOT_ENABLED )); then wanted+=(aiot-bot); else skipped+=(aiot-bot); fi

was_active=()
for unit in "${wanted[@]}"; do
  if systemctl is-active --quiet "$unit"; then was_active+=("$unit"); fi
done

if (( ${#skipped[@]} > 0 )); then
  if (( NO_FAN )); then
    sudo systemctl disable --now aiot-controller >/dev/null 2>&1 || true
    ok "--no-fan: đã dừng aiot-controller và tắt tự khởi động (quạt tắt)"
  fi
  if (( ! BOT_ENABLED )); then
    sudo systemctl stop aiot-bot >/dev/null 2>&1 || true
    warn "aiot-bot không chạy (thiếu token/chat id)"
  fi
fi

sudo systemctl enable --now "${wanted[@]}" "${TIMERS[@]}"
if (( ${#was_active[@]} > 0 )); then
  sudo systemctl restart "${was_active[@]}"
  ok "đã khởi động lại để nạp code mới: ${was_active[*]}"
fi
ok "đang chạy: ${wanted[*]} ${TIMERS[*]}"

# ---------------- 6. bảng kiểm tra ----------------
section "Chờ ${WAIT_S} giây cho collector đọc mẫu đầu tiên"
if [[ -t 1 ]]; then
  for (( s = WAIT_S; s > 0; s-- )); do
    printf '\r    còn %2d giây ' "$s"
    sleep 1
  done
  printf '\r    xong.          \n'
else
  sleep "$WAIT_S"
fi

set +e
run_healthcheck
code=$?
set -e

if (( REBOOT_NEEDED )); then
  printf '\n%s!! Đã đổi cấu hình I2C/UART: cần khởi động lại Pi (sudo reboot); dịch vụ tự chạy lại sau khi bật.%s\n' "$C_YELLOW" "$C_RESET"
fi
exit "$code"
