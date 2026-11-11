#!/usr/bin/env bash
# Bật toàn bộ trạm AIoT trên Raspberry Pi bằng một lệnh (chạy trên Pi, user admin).
#   bash ~/BTL_IOT/scripts/pi_up.sh              cài/cập nhật rồi bật mọi dịch vụ
#   bash ~/BTL_IOT/scripts/pi_up.sh --no-fan     như trên nhưng không chạy aiot-controller
#   bash ~/BTL_IOT/scripts/pi_up.sh --stop       dừng mọi dịch vụ và timer (quạt tắt)
#   bash ~/BTL_IOT/scripts/pi_up.sh --status     chỉ in bảng kiểm tra (không cần sudo)
# Chạy lại bao nhiêu lần cũng được: bước nào đã xong thì bỏ qua. Dịch vụ chỉ được khởi động
# lại khi code, cấu hình, .env, unit hoặc thư viện đổi (khởi động lại controller khi đang
# BẢO VỆ làm relay tắt rồi bật). Không bao giờ in giá trị trong .env.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

EXPECTED_DIR="/home/admin/BTL_IOT"
ENV_KEYS=(TELEGRAM_TOKEN GEMINI_API_KEY ALLOWED_CHAT_IDS MQTT_PASSWORD)
APT_PACKAGES=(i2c-tools python3-venv sqlite3)
BUILD_PACKAGES=(build-essential python3-dev swig liblgpio-dev)
GROUPS_NEEDED=(gpio i2c dialout video)
TIMEZONE="Asia/Ho_Chi_Minh"
SERVICES=(aiot-collector aiot-controller aiot-bot)
TIMERS=(aiot-forecast.timer aiot-morning.timer aiot-weather.timer)
VENV_PY="$REPO_DIR/venv/bin/python"
REQ_STAMP="$REPO_DIR/venv/.requirements-pi.sha256"
CODE_STAMP="$REPO_DIR/venv/.deployed-code.sha256"
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
Cách dùng: bash scripts/pi_up.sh [--no-fan] [--force] | --stop | --status | --help

  (không cờ)   cài phần còn thiếu, cập nhật unit systemd, bật dịch vụ (khởi động lại khi
               code/cấu hình/thư viện đổi), rồi in bảng kiểm tra sức khoẻ
  --no-fan     như trên nhưng dừng và tắt tự khởi động aiot-controller (quạt không bị điều khiển)
  --force      cho chạy khi repo không ở /home/admin/BTL_IOT (unit systemd vẫn dùng đường dẫn đó)
  --stop       dừng mọi dịch vụ và timer, tắt tự khởi động aiot-controller (quạt tắt)
  --status     chỉ in bảng kiểm tra sức khoẻ, không cần sudo
  --help       in hướng dẫn này
EOF
}

MODE="up"
NO_FAN=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --no-fan) NO_FAN=1 ;;
    --force) FORCE=1 ;;
    --stop) MODE="stop" ;;
    --status) MODE="status" ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "không hiểu tuỳ chọn '$arg'" ;;
  esac
done

# ---------------- tiện ích ----------------
# Khoá có giá trị khác rỗng trong .env không (không in giá trị). Cùng quy tắc với
# aiot.config.read_env_file: bỏ BOM (Notepad), "export ", ngoặc kép/đơn, chú thích " #...",
# CR của Windows.
env_has() {
  local key="$1" line value
  line="$(sed -e $'1s/^\xef\xbb\xbf//' .env \
    | grep -E "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" | tail -n 1 || true)"
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

pkg_installed() {
  # shellcheck disable=SC2016  # ${Status} là định dạng của dpkg-query, không phải biến bash
  [[ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null || true)" == "install ok installed" ]]
}

py_imports() { "$VENV_PY" -c "import $1" >/dev/null 2>&1; }

unit_enabled() { [[ "$(systemctl is-enabled "$1" 2>/dev/null || true)" == "enabled" ]]; }

APT_UPDATED=0
apt_update_once() {
  if (( ! APT_UPDATED )); then
    info "Cập nhật danh sách gói (apt-get update)..."
    sudo apt-get update -qq || warn "apt-get update lỗi (mất mạng?)"
    APT_UPDATED=1
  fi
}

apt_install() {
  apt_update_once
  info "Cài: $*"
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
}

# Băm mọi thứ dịch vụ đọc lúc khởi động: đổi thì phải khởi động lại để nạp
code_hash() {
  { find aiot deploy -type f ! -path '*/__pycache__/*' -print0
    printf '%s\0' config.yaml requirements-pi.txt .env
  } | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1
}

SUDO_KEEPALIVE_PID=""
TMP_FILES=()
# shellcheck disable=SC2329  # được gọi qua trap EXIT
cleanup() {
  if [[ -n "$SUDO_KEEPALIVE_PID" ]]; then kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true; fi
  if (( ${#TMP_FILES[@]} > 0 )); then rm -f "${TMP_FILES[@]}"; fi
}
trap cleanup EXIT

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
if [[ "$REPO_DIR" != "$EXPECTED_DIR" ]]; then
  (( FORCE )) || die "repo đang ở $REPO_DIR nhưng các unit systemd chạy code ở $EXPECTED_DIR. Chuyển repo về đó, hoặc thêm --force nếu biết mình đang làm gì."
  warn "--force: repo ở $REPO_DIR, unit systemd vẫn dùng $EXPECTED_DIR"
fi
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
ok "đã có quyền sudo"

# ---------------- --stop ----------------
if [[ "$MODE" == "stop" ]]; then
  section "Dừng dịch vụ"
  sudo systemctl stop "${TIMERS[@]}" "${SERVICES[@]}" || warn "có unit không dừng được"
  sudo systemctl disable aiot-controller >/dev/null 2>&1 || true
  ok "đã dừng ${TIMERS[*]} ${SERVICES[*]}"
  info "aiot-controller dừng thì đưa GPIO23 về LOW: quạt tắt; đã tắt tự khởi động của nó."
  info "Các dịch vụ khác vẫn tự chạy khi bật lại Pi. Bật lại tất cả: bash scripts/pi_up.sh"
  exit 0
fi

# ---------------- 3. cài đặt lần đầu ----------------
REBOOT_NEEDED=0
LIBS_CHANGED=0

section "Gói hệ thống"
to_install=()
for pkg in "${APT_PACKAGES[@]}"; do
  pkg_installed "$pkg" || to_install+=("$pkg")
done
# DHT11 cần libgpiod (libgpiod_pulsein của Blinka); libgpiod2 là bản đã thử trên mạch
if pkg_installed libgpiod2; then
  :
elif pkg_installed libgpiod3; then
  warn "chỉ có libgpiod3 (chưa thử với DHT11); nếu DHT11 lỗi liên tục, cài libgpiod2"
else
  apt_update_once
  if apt-cache show libgpiod2 >/dev/null 2>&1; then
    to_install+=(libgpiod2)
  elif apt-cache show libgpiod3 >/dev/null 2>&1; then
    to_install+=(libgpiod3)
    warn "hệ điều hành này không có libgpiod2, dùng libgpiod3 (chưa thử với DHT11)"
  else
    warn "không tìm thấy libgpiod2/libgpiod3: DHT11 có thể đọc lỗi 'Checksum did not validate'"
  fi
fi
if (( ${#to_install[@]} > 0 )); then
  apt_install "${to_install[@]}" || warn "cài gói hệ thống lỗi"
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
  python3 -m venv venv || die "không tạo được venv (cần gói python3-venv)"
  "$VENV_PY" -m pip install --upgrade pip -q || warn "không nâng cấp được pip"
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
  info "Cài requirements-pi.txt (lần đầu mất vài phút, lâu hơn nếu phải build lgpio/sysv_ipc)..."
  LIBS_CHANGED=1
  req_file="requirements-pi.txt"
  if py_imports cv2; then
    # opencv-python có sẵn: cài thêm opencv-python-headless sẽ ghi đè thư mục cv2 và làm hỏng import
    req_file="$(mktemp)"
    TMP_FILES+=("$req_file")
    grep -v -E '^[[:space:]]*opencv-python-headless' requirements-pi.txt > "$req_file"
    info "venv đã có cv2: bỏ qua opencv-python-headless"
  fi
  pip_ok=1
  if ! "$VENV_PY" -m pip install -q -r "$req_file"; then
    warn "pip lỗi, thường do lgpio/sysv_ipc phải build từ source; cài công cụ build + wheel lgpio của Adafruit rồi thử lại"
    apt_install "${BUILD_PACKAGES[@]}" || warn "không cài được công cụ build"
    install_lgpio_wheel || warn "không cài được wheel lgpio"
    "$VENV_PY" -m pip install -q -r "$req_file" || pip_ok=0
  fi
  if ! py_imports lgpio; then
    install_lgpio_wheel || true
  fi
  if ! py_imports sysv_ipc; then
    "$VENV_PY" -m pip install -q sysv_ipc || true
  fi
  for mod in lgpio sysv_ipc; do
    if ! py_imports "$mod"; then
      warn "import $mod vẫn lỗi (docs/van_hanh_chi_tiet.md, Phụ lục C)"
      pip_ok=0
    fi
  done
  py_imports cv2 || warn "import cv2 lỗi: /photo và ảnh cảnh báo sẽ không chụp được"
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
    sudo install -m 644 "$src" "$dest" || die "không chép được $src vào /etc/systemd/system"
    units_changed=1
    info "cập nhật $(basename "$src")"
  fi
done
if (( units_changed )); then
  sudo systemctl daemon-reload || die "systemctl daemon-reload lỗi"
  ok "đã nạp lại unit (daemon-reload)"
else
  ok "unit đã mới nhất"
fi

wanted=(aiot-collector)
if (( ! NO_FAN )); then wanted+=(aiot-controller); fi
if (( BOT_ENABLED )); then wanted+=(aiot-bot); fi

was_active=()
for unit in "${wanted[@]}"; do
  if systemctl is-active --quiet "$unit"; then was_active+=("$unit"); fi
done

if (( NO_FAN )); then
  sudo systemctl disable --now aiot-controller >/dev/null 2>&1 || true
  ok "--no-fan: đã dừng aiot-controller và tắt tự khởi động (quạt tắt)"
fi
if (( ! BOT_ENABLED )); then
  sudo systemctl stop aiot-bot >/dev/null 2>&1 || true
  warn "aiot-bot không chạy (thiếu token/chat id)"
fi

sudo systemctl enable --now "${wanted[@]}" "${TIMERS[@]}" \
  || die "không bật được dịch vụ; xem: journalctl -u aiot-collector -n 30"

new_code_hash="$(code_hash)"
old_code_hash="$(cat "$CODE_STAMP" 2>/dev/null || true)"
if [[ "$new_code_hash" != "$old_code_hash" ]] || (( units_changed || LIBS_CHANGED )); then
  if (( ${#was_active[@]} > 0 )); then
    sudo systemctl restart "${was_active[@]}" || die "khởi động lại dịch vụ lỗi"
    ok "code/cấu hình/thư viện đã đổi: đã khởi động lại ${was_active[*]}"
  fi
else
  ok "code không đổi: không khởi động lại dịch vụ đang chạy"
fi
printf '%s\n' "$new_code_hash" > "$CODE_STAMP"
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
