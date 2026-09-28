#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Refresh WSL port forwarding for Lampy stack.
.DESCRIPTION
    WSL2 IP addresses change on reboot. This script re-discovers the
    lampy distro IP and re-creates the Windows port proxies so Windows
    apps (pgAdmin, browsers, mail clients) can reach WSL services via
    localhost.
    
    Run this after WSL reboots or if localhost connections stop working.
    
    Forwarded ports:
      5432 - PostgreSQL (pgAdmin: localhost:5432)
      80   - Apache HTTPD (browser: http://localhost/)
      8000 - Forum app (browser: http://localhost:8000/)
      1143 - James IMAP (mail client: localhost:1143)
      2587 - James SMTP (mail client: localhost:2587)
#>

Write-Host "Finding WSL lampy distro IP..." -ForegroundColor Cyan
$wslIp = wsl -d lampy -u root -- python3 -c "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print(s.getsockname()[0])" 2>$null

if (-not $wslIp) {
    Write-Host "ERROR: Could not get WSL IP. Is the lampy distro running?" -ForegroundColor Red
    exit 1
}

$wslIp = $wslIp.Trim()
Write-Host "WSL IP: $wslIp" -ForegroundColor Green

$ports = @{
    5432 = "PostgreSQL"
    80   = "Apache HTTPD"
    8000 = "Forum app"
    1143 = "James IMAP"
    2587 = "James SMTP"
}

Write-Host "`nSetting up port forwards..." -ForegroundColor Cyan
foreach ($port in $ports.Keys) {
    # Remove old proxy if exists (ignore errors)
    netsh interface portproxy delete v4tov4 listenport=$port listenaddress=127.0.0.1 2>$null | Out-Null
    
    # Add new proxy
    netsh interface portproxy add v4tov4 listenport=$port listenaddress=127.0.0.1 connectport=$port connectaddress=$wslIp | Out-Null
    
    Write-Host "  127.0.0.1:$port -> $wslIp`:$port ($($ports[$port]))" -ForegroundColor White
}

Write-Host "`nDone! Windows apps can now use localhost to reach WSL services." -ForegroundColor Green
Write-Host "`npgAdmin connection:" -ForegroundColor Yellow
Write-Host "  Host: localhost"
Write-Host "  Port: 5432"
Write-Host "  Database: forum"
Write-Host "  Username: dbadmin"
Write-Host "  (Password: see /etc/supervisor/conf.d/lampy.conf on WSL)"
Write-Host "`nApache: http://localhost/" -ForegroundColor Yellow
Write-Host "Forum:  http://localhost:8000/" -ForegroundColor Yellow
