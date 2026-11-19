# Menjalankan bot + dashboard yang bisa dibuka dari HP lewat Cloudflare Quick Tunnel (gratis, tanpa akun).
# Alamat https://xxxx.trycloudflare.com BERUBAH tiap skrip ini dijalankan; kirim /dashboard lagi untuk tautan baru.
# Pakai:  powershell -ExecutionPolicy Bypass -File scripts\jalankan-publik.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$cf = @("C:\Program Files (x86)\cloudflared\cloudflared.exe", "C:\Program Files\cloudflared\cloudflared.exe") |
      Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $cf) { throw "cloudflared belum terpasang: winget install Cloudflare.cloudflared" }

# Hentikan bot & tunnel lama (hanya milik proyek ini).
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match "-m keuangan" -or ($_.Name -eq "cloudflared.exe" -and $_.CommandLine -match "trycloudflare|--url")
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 2

$port = 8765
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    $line = Get-Content $envFile | Where-Object { $_ -match "^\s*DASHBOARD_PORT\s*=\s*(\d+)" } | Select-Object -First 1
    if ($line -match "=\s*(\d+)") { $port = [int]$Matches[1] }
}

$log = Join-Path $root "data\tunnel.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
Remove-Item $log -ErrorAction SilentlyContinue
Start-Process -FilePath $cf -ArgumentList "tunnel", "--no-autoupdate", "--logfile", $log, "--url", "http://127.0.0.1:$port" -WindowStyle Hidden

$url = $null
for ($i = 0; $i -lt 40 -and -not $url; $i++) {
    Start-Sleep 1
    if (Test-Path $log) {
        $m = Select-String -Path $log -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -First 1
        if ($m) { $url = $m.Matches[0].Value }
    }
}
if (-not $url) { throw "Alamat tunnel tidak muncul. Lihat $log" }

# Variabel lingkungan menang atas .env (load_dotenv tidak menimpa), jadi .env tak perlu diubah.
$env:DASHBOARD_PUBLIC_URL = $url
$env:DASHBOARD_TRUST_PROXY = "true"
Start-Process -FilePath (Join-Path $root ".venv\Scripts\pythonw.exe") -ArgumentList "-m", "keuangan" `
    -WorkingDirectory $root -WindowStyle Hidden
Write-Host "Dashboard publik: $url"
Write-Host "Kirim /dashboard di Telegram untuk tautan masuk (berlaku 10 menit, sekali pakai)."
