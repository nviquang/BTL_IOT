<#
.SYNOPSIS
  Triển khai qua SSH và chạy scripts/pi_up.sh trên Raspberry Pi.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\up.ps1
  powershell -ExecutionPolicy Bypass -File scripts\up.ps1 -NoFan
  powershell -ExecutionPolicy Bypass -File scripts\up.ps1 -Status
  powershell -ExecutionPolicy Bypass -File scripts\up.ps1 -StopPi
#>
param(
    [string]$PiHost = "",
    [switch]$NoDeploy,
    [switch]$NoFan,
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
# IP của Pi đổi theo mạng: chấp nhận host key của máy mới gặp lần đầu (vẫn từ chối khi key
# của một host đã biết bị đổi)
$SshOpts = @("-o", "StrictHostKeyChecking=accept-new")
$RemoteTar = "/tmp/btl_iot_deploy.tar"
$DeployPaths = @("aiot", "deploy", "scripts", "config.yaml", "requirements-pi.txt",
    "test_all.py", "README.md", "docs", "training/__init__.py", "training/demo_data.py",
    "training/data/demo_real.json")
# Mô hình đã huấn luyện (models/ không commit): chép riêng nếu có trên laptop
$ModelFiles = @("source_logreg.json", "source_logreg_summary.json")
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

function Invoke-Ssh([string]$PiTarget, [string]$Command) {
    # Trả về mã thoát của ssh; lệnh từ xa không được chứa dấu ngoặc kép (PS 5.1 truyền sai)
    & ssh @SshOpts -o ConnectTimeout=10 $PiTarget $Command | Out-Host
    return $LASTEXITCODE
}

function Find-Pi {
    $candidates = @()
    if ($PiHost) { $candidates = @($PiHost) } else { $candidates = $DefaultHosts }
    foreach ($h in $candidates) {
        Write-Info "Thử ssh $PiUser@$h ..."
        $out = & ssh @SshOpts -o BatchMode=yes -o ConnectTimeout=5 "$PiUser@$h" "true" 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Pi ở $h"
            return $h
        }
        $lines = @($out | ForEach-Object { "$_" } | Where-Object { $_.Trim() })
        if ($lines.Count -gt 0) {
            Write-Warn "ssh: $($lines[-1].Trim())"
        }
    }
    Stop-WithError ("không SSH được tới Pi ($($candidates -join ', ')). Kiểm tra Pi đã bật, cùng Wi-Fi, " +
        "đã cài SSH key (ssh $PiUser@<IP> phải vào không hỏi mật khẩu), hoặc chỉ định -PiHost <IP>.")
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
    & scp @SshOpts -q $tar "${PiTarget}:$RemoteTar"
    if ($LASTEXITCODE -ne 0) { Stop-WithError "scp gói code lỗi" }
    & scp @SshOpts -q (Join-Path $RepoDir ".env") "${PiTarget}:BTL_IOT/.env"
    if ($LASTEXITCODE -ne 0) { Stop-WithError "scp .env lỗi" }
    Remove-Item $tar -Force
    $models = @($ModelFiles | ForEach-Object { Join-Path $RepoDir "models\$_" } | Where-Object { Test-Path $_ })
    if ($models.Count -gt 0) {
        if ((Invoke-Ssh $PiTarget "mkdir -p ~/BTL_IOT/models") -ne 0) { Stop-WithError "không tạo được ~/BTL_IOT/models" }
        & scp @SshOpts -q @models "${PiTarget}:BTL_IOT/models/"
        if ($LASTEXITCODE -ne 0) { Stop-WithError "scp mô hình lỗi" }
        Write-Ok "đã chép $($models.Count) file mô hình lên ~/BTL_IOT/models"
    } else {
        Write-Warn "không có models/source_logreg.json: controller dùng luật ngưỡng PM2.5 (chạy training/train_logreg.py)"
    }

    $remote = "cd ~/BTL_IOT && tar -xmf $RemoteTar && rm -f $RemoteTar && chmod 600 .env && chmod +x scripts/pi_up.sh"
    if ((Invoke-Ssh $PiTarget $remote) -ne 0) { Stop-WithError "giải nén code trên Pi lỗi" }
    Write-Ok "đã chép code + .env lên ~/BTL_IOT (chmod 600 .env)"
}

# ---------------- chương trình chính ----------------
$requiredTools = @("ssh")
if (-not ($NoDeploy -or $Status -or $StopPi)) { $requiredTools += @("scp", "git") }
foreach ($tool in $requiredTools) {
    if (-not (Test-Command $tool)) {
        Stop-WithError "không tìm thấy lệnh '$tool'. Cài Git for Windows và OpenSSH Client (Settings > Optional features)."
    }
}

if ($Status) {
    Write-Step "Trạng thái trạm trên Pi"
    $pi = Find-Pi
    & ssh @SshOpts -t "$PiUser@$pi" "bash ~/BTL_IOT/scripts/pi_up.sh --status"
    exit $LASTEXITCODE
}

if ($StopPi) {
    Write-Step "Dừng dịch vụ trên Pi"
    $pi = Find-Pi
    & ssh @SshOpts -t "$PiUser@$pi" "bash ~/BTL_IOT/scripts/pi_up.sh --stop"
    exit $LASTEXITCODE
}

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
if ($NoFan) { $piArgs += " --no-fan" }
& ssh @SshOpts -t $target "bash ~/BTL_IOT/scripts/pi_up.sh$piArgs"
$piCode = $LASTEXITCODE

Write-Host ""
if ($piCode -eq 0) {
    Write-Host "Xong: trạm đang chạy. Gửi /help cho bot Telegram để thử." -ForegroundColor Green
} else {
    Write-Host "Trạm đã bật nhưng có mục quan trọng chưa đạt (mã $piCode). Xem bảng kiểm tra ở trên." -ForegroundColor Yellow
}
exit $piCode
