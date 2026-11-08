<#
.SYNOPSIS
  Bật toàn bộ trạm AIoT từ laptop bằng một lệnh: Mosquitto, đưa code lên Pi, chạy
  scripts/pi_up.sh trên Pi, rồi mở dashboard Streamlit.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\up.ps1
  powershell -ExecutionPolicy Bypass -File scripts\up.ps1 -NoFan
  powershell -ExecutionPolicy Bypass -File scripts\up.ps1 -Status
  powershell -ExecutionPolicy Bypass -File scripts\up.ps1 -Stop -StopPi
#>
param(
    [string]$PiHost = "",
    [switch]$NoDeploy,
    [switch]$NoFan,
    [switch]$Stop,
    [switch]$StopPi,
    [switch]$Status
)

# Viết cho Windows PowerShell 5.1: không dùng &&, ||, toán tử ba ngôi hay ??.
# Không đặt ErrorActionPreference = Stop: trong PS 5.1, lệnh ngoài (ssh, git) ghi stderr khi
# có chuyển hướng 2> sẽ thành lỗi dừng script. Mọi lỗi được kiểm tra bằng $LASTEXITCODE.
$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$RepoDir = Split-Path -Parent $PSScriptRoot
Set-Location $RepoDir

$PiUser = "admin"
$DefaultHosts = @("10.157.117.41", "pi.local")
$MosqConf = Join-Path $env:USERPROFILE "mosq\mosquitto.conf"
$VenvPy = Join-Path $RepoDir ".venv\Scripts\python.exe"
$DashboardUrl = "http://localhost:8501"
$RemoteTar = "/tmp/btl_iot_deploy.tar"
$DeployPaths = @("aiot", "deploy", "scripts", "config.yaml", "requirements-pi.txt", "test_all.py", "README.md", "docs")
$DashboardModules = "import streamlit, paho.mqtt, yaml, pandas, matplotlib"
$DashboardPackages = @("streamlit", "paho-mqtt", "PyYAML", "pandas", "matplotlib")

function Write-Step([string]$Text) { Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    [ok] $Text" -ForegroundColor Green }
function Write-Info([string]$Text) { Write-Host "    $Text" }
function Write-Warn([string]$Text) { Write-Host "    [!] $Text" -ForegroundColor Yellow }
function Stop-WithError([string]$Text) {
    Write-Host ""
    Write-Host "Lỗi: $Text" -ForegroundColor Red
    exit 1
}

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-PortListening([int]$Port) {
    try {
        $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        return [bool]$c
    } catch {
        return $false
    }
}

function Invoke-Ssh([string]$PiTarget, [string]$Command) {
    # Trả về mã thoát của ssh; lệnh từ xa không được chứa dấu ngoặc kép (PS 5.1 truyền sai)
    & ssh -o ConnectTimeout=10 $PiTarget $Command | Out-Host
    return $LASTEXITCODE
}

function Find-Pi {
    $candidates = @()
    if ($PiHost) { $candidates = @($PiHost) } else { $candidates = $DefaultHosts }
    foreach ($h in $candidates) {
        Write-Info "Thử ssh $PiUser@$h ..."
        & ssh -o BatchMode=yes -o ConnectTimeout=5 "$PiUser@$h" "true" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Pi ở $h"
            return $h
        }
    }
    Stop-WithError ("không SSH được tới Pi ($($candidates -join ', ')). Kiểm tra Pi đã bật, cùng Wi-Fi, " +
        "đã cài SSH key (ssh $PiUser@<IP> phải vào không hỏi mật khẩu), hoặc chỉ định -PiHost <IP>.")
}

function Get-LaptopIp([string]$PiTarget) {
    # IP laptop như Pi nhìn thấy (đúng giao diện mạng đi tới Pi)
    $out = & ssh -o BatchMode=yes -o ConnectTimeout=5 $PiTarget 'echo $SSH_CLIENT' 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) {
        $first = ([string]$out).Trim().Split(" ")[0]
        if ($first -match '^\d{1,3}(\.\d{1,3}){3}$') { return $first }
    }
    # Dự phòng: IPv4 của giao diện có route mặc định
    try {
        $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction Stop |
            Sort-Object { $_.RouteMetric + $_.InterfaceMetric } | Select-Object -First 1
        $addr = Get-NetIPAddress -InterfaceIndex $route.ifIndex -AddressFamily IPv4 -ErrorAction Stop |
            Select-Object -First 1
        return $addr.IPAddress
    } catch {
        return $null
    }
}

function Start-Broker {
    Write-Step "Broker MQTT (Mosquitto)"
    if (Test-PortListening 1883) {
        Write-Ok "đã có broker nghe cổng 1883"
        return
    }
    if (-not (Test-Path $MosqConf)) {
        Write-Warn "không có ${MosqConf}: chạy tiếp không có broker (dashboard sẽ không nhận dữ liệu; README chi tiết mục 2.2)"
        return
    }
    $exe = $null
    $cmd = Get-Command mosquitto -ErrorAction SilentlyContinue
    if ($cmd) { $exe = $cmd.Source } elseif (Test-Path "C:\Program Files\Mosquitto\mosquitto.exe") {
        $exe = "C:\Program Files\Mosquitto\mosquitto.exe"
    }
    if (-not $exe) {
        Write-Warn "không tìm thấy mosquitto.exe: chạy tiếp không có broker"
        return
    }
    Start-Process -FilePath $exe -ArgumentList @("-c", "`"$MosqConf`"", "-v") -WindowStyle Minimized | Out-Null
    Start-Sleep -Seconds 2
    if (Test-PortListening 1883) {
        Write-Ok "đã bật Mosquitto (cửa sổ thu nhỏ)"
    } else {
        Write-Warn "Mosquitto chưa nghe cổng 1883: mở cửa sổ Mosquitto để xem lỗi"
    }
}

function Deploy-ToPi([string]$PiTarget) {
    Write-Step "Đưa code lên Pi"
    $dirty = & git status --porcelain
    if ($LASTEXITCODE -ne 0) { Stop-WithError "git status lỗi (thư mục này có phải repo git?)" }
    if ($dirty) {
        Write-Warn "có thay đổi chưa commit: KHÔNG được đưa lên Pi (chỉ bản HEAD được gửi)"
    }
    if (-not (Test-Path (Join-Path $RepoDir ".env"))) {
        Stop-WithError "không có file .env trong $RepoDir. Chép .env.example thành .env và điền 4 khoá (README mục Chạy nhanh)."
    }
    $tar = Join-Path $env:TEMP "btl_iot_deploy.tar"
    if (Test-Path $tar) { Remove-Item $tar -Force }
    # Ghi tar thẳng ra file: không đẩy dữ liệu nhị phân qua pipe của PowerShell 5.1
    $gitArgs = @("archive", "-o", $tar, "HEAD") + $DeployPaths
    & git @gitArgs
    if ($LASTEXITCODE -ne 0) { Stop-WithError "git archive lỗi" }
    $sizeKb = [math]::Round((Get-Item $tar).Length / 1KB)
    Write-Ok "đã đóng gói HEAD ($sizeKb KB)"

    if ((Invoke-Ssh $PiTarget "mkdir -p ~/BTL_IOT") -ne 0) { Stop-WithError "không tạo được ~/BTL_IOT trên Pi" }
    & scp -q $tar "${PiTarget}:$RemoteTar"
    if ($LASTEXITCODE -ne 0) { Stop-WithError "scp gói code lỗi" }
    & scp -q (Join-Path $RepoDir ".env") "${PiTarget}:BTL_IOT/.env"
    if ($LASTEXITCODE -ne 0) { Stop-WithError "scp .env lỗi" }
    Remove-Item $tar -Force

    $ip = Get-LaptopIp $PiTarget
    $sedCmd = ""
    if ($ip) {
        # Chỉ sửa bản config.yaml trên Pi, không sửa file trong git
        $sedCmd = " && sed -i -E 's/^(  host:[[:space:]]*)[^[:space:]#]+/\1$ip/' config.yaml"
    }
    $remote = "cd ~/BTL_IOT && tar -xf $RemoteTar && rm -f $RemoteTar && chmod 600 .env && chmod +x scripts/pi_up.sh$sedCmd"
    if ((Invoke-Ssh $PiTarget $remote) -ne 0) { Stop-WithError "giải nén code trên Pi lỗi" }
    Write-Ok "đã chép code + .env lên ~/BTL_IOT (chmod 600 .env)"
    if ($ip) {
        Write-Ok "mqtt.host trên Pi = $ip (IP laptop)"
    } else {
        Write-Warn "không xác định được IP laptop: giữ nguyên mqtt.host trong config.yaml trên Pi"
    }
}

function Start-Dashboard {
    Write-Step "Dashboard Streamlit"
    if (Test-PortListening 8501) {
        Write-Ok "dashboard đã chạy"
        Start-Process $DashboardUrl | Out-Null
        return
    }
    if (-not (Test-Path $VenvPy)) {
        if (-not (Test-Command python)) {
            Write-Warn "không có python trên PATH: bỏ qua dashboard"
            return
        }
        Write-Info "Tạo .venv ..."
        & python -m venv (Join-Path $RepoDir ".venv")
        if ($LASTEXITCODE -ne 0) { Write-Warn "tạo .venv lỗi: bỏ qua dashboard"; return }
    }
    & $VenvPy -c $DashboardModules 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Info "Cài thư viện dashboard ($($DashboardPackages -join ', ')) ..."
        & $VenvPy -m pip install -q @DashboardPackages
        if ($LASTEXITCODE -ne 0) { Write-Warn "pip lỗi: bỏ qua dashboard"; return }
    }
    Start-Process -FilePath $VenvPy -WorkingDirectory $RepoDir -WindowStyle Minimized `
        -ArgumentList @("-m", "streamlit", "run", "server_app.py", "--server.headless", "true") | Out-Null
    for ($i = 0; $i -lt 30; $i++) {
        if (Test-PortListening 8501) { break }
        Start-Sleep -Seconds 1
    }
    if (Test-PortListening 8501) {
        Write-Ok "dashboard ở $DashboardUrl (giữ tab mở để nhận dữ liệu)"
        Start-Process $DashboardUrl | Out-Null
    } else {
        Write-Warn "Streamlit chưa mở cổng 8501: xem cửa sổ Streamlit (thu nhỏ)"
    }
}

function Stop-Local {
    Write-Step "Dừng dashboard và Mosquitto trên laptop"
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='streamlit.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*streamlit*" -and $_.CommandLine -like "*server_app.py*" })
    if ($procs.Count -gt 0) {
        foreach ($p in $procs) {
            try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { }
        }
        Write-Ok "đã dừng Streamlit ($($procs.Count) tiến trình)"
    } else {
        Write-Ok "Streamlit không chạy"
    }
    $mosq = @(Get-Process mosquitto -ErrorAction SilentlyContinue)
    if ($mosq.Count -gt 0) {
        try {
            $mosq | Stop-Process -Force -ErrorAction Stop
            Write-Ok "đã dừng Mosquitto"
        } catch {
            Write-Warn "không dừng được Mosquitto (có thể đang chạy như dịch vụ Windows, cần quyền admin)"
        }
    } else {
        Write-Ok "Mosquitto không chạy"
    }
}

# ---------------- chương trình chính ----------------
foreach ($tool in @("ssh", "scp", "git")) {
    if (-not (Test-Command $tool)) {
        Stop-WithError "không tìm thấy lệnh '$tool'. Cài Git for Windows và OpenSSH Client (Settings > Optional features)."
    }
}

if ($Stop) {
    Stop-Local
    if ($StopPi) {
        Write-Step "Dừng dịch vụ trên Pi"
        $pi = Find-Pi
        & ssh -t "$PiUser@$pi" "bash ~/BTL_IOT/scripts/pi_up.sh --stop"
        if ($LASTEXITCODE -ne 0) { Stop-WithError "pi_up.sh --stop lỗi (mã $LASTEXITCODE)" }
    } else {
        Write-Info "Dịch vụ trên Pi vẫn chạy (thêm -StopPi để dừng cả Pi)."
    }
    exit 0
}

if ($Status) {
    Write-Step "Trạng thái trạm trên Pi"
    $pi = Find-Pi
    & ssh -t "$PiUser@$pi" "bash ~/BTL_IOT/scripts/pi_up.sh --status"
    exit $LASTEXITCODE
}

Start-Broker

Write-Step "Tìm Raspberry Pi"
$pi = Find-Pi
$target = "$PiUser@$pi"

if (-not $NoDeploy) {
    Deploy-ToPi $target
} else {
    Write-Info "-NoDeploy: dùng code đang có trên Pi"
}

Write-Step "Chạy scripts/pi_up.sh trên Pi (có thể hỏi mật khẩu sudo của admin)"
$piArgs = ""
if ($NoFan) { $piArgs = " --no-fan" }
& ssh -t $target "bash ~/BTL_IOT/scripts/pi_up.sh$piArgs"
$piCode = $LASTEXITCODE

Start-Dashboard

Write-Host ""
if ($piCode -eq 0) {
    Write-Host "Xong: trạm đang chạy. Gửi /help cho bot Telegram để thử." -ForegroundColor Green
} else {
    Write-Host "Trạm đã bật nhưng có mục quan trọng chưa đạt (mã $piCode). Xem bảng kiểm tra ở trên." -ForegroundColor Yellow
}
exit $piCode
