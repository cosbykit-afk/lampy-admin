"""Windows tools integration — port forwarding and desktop app connections.

The console runs inside WSL. Windows port proxies (netsh) are managed
via SSH to the Windows host.
"""

import subprocess
import os

# Port -> service name mapping
PORTS = {
    5432: "PostgreSQL",
    80: "Apache HTTPD",
    8000: "Forum app",
    1143: "James IMAP",
    2587: "James SMTP",
}

# Windows host via Tailscale (same as the VM uses to reach Toetop)
WINDOWS_HOST = "100.124.30.78"
WINDOWS_USER = "kitco"
SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")
PROXY_SCRIPT = os.path.expanduser("~/workspace/toetop-ssh-proxy.sh")

def _ssh_windows(cmd, timeout=30):
    """Run a command on Windows via SSH."""
    ssh_cmd = [
        "ssh", "-i", SSH_KEY,
        "-o", f"ProxyCommand={PROXY_SCRIPT} %h %p",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={timeout}",
        f"{WINDOWS_USER}@{WINDOWS_HOST}",
        cmd
    ]
    try:
        result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout+10)
        return result.returncode == 0, result.stdout, result.stderr
    except Exception as e:
        return False, "", str(e)

def get_wsl_ip():
    """Get the current WSL lampy distro IP (run inside WSL)."""
    try:
        result = subprocess.run(
            ["python3", "-c",
             "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
             "s.connect(('8.8.8.8',80)); print(s.getsockname()[0])"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None

def get_proxy_status():
    """Get portproxy status from Windows."""
    ok, stdout, stderr = _ssh_windows("netsh interface portproxy show all")
    if not ok:
        return {"ok": False, "error": "Could not reach Windows host"}
    
    # Parse netsh output
    proxies = []
    wsl_ip = get_wsl_ip()
    
    for line in stdout.split("\n"):
        parts = line.split()
        # Look for lines like: 127.0.0.1  5432  172.25.200.76  5432
        if len(parts) >= 4 and parts[0] == "127.0.0.1":
            try:
                port = int(parts[1])
                target = f"{parts[2]}:{parts[3]}"
                service = PORTS.get(port, "Unknown")
                proxies.append({
                    "port": port,
                    "service": service,
                    "target": target,
                    "ok": True,  # If it's in netsh, the proxy exists
                })
            except (ValueError, IndexError):
                pass
    
    # Add missing ports as not configured
    configured_ports = {p["port"] for p in proxies}
    for port, service in PORTS.items():
        if port not in configured_ports:
            proxies.append({
                "port": port,
                "service": service,
                "target": "not configured",
                "ok": False,
            })
    
    proxies.sort(key=lambda x: x["port"])
    return {"ok": True, "wsl_ip": wsl_ip, "proxies": proxies}

def refresh_proxies():
    """Refresh Windows port proxies via SSH."""
    wsl_ip = get_wsl_ip()
    if not wsl_ip:
        return {"ok": False, "error": "Could not determine WSL IP"}
    
    # Build PowerShell command to set up proxies
    ps_cmd = f"$wslIp='{wsl_ip}'; "
    for port in PORTS:
        ps_cmd += f"netsh interface portproxy delete v4tov4 listenport={port} listenaddress=127.0.0.1 2>$null | Out-Null; "
        ps_cmd += f"netsh interface portproxy add v4tov4 listenport={port} listenaddress=127.0.0.1 connectport={port} connectaddress=$wslIp | Out-Null; "
    ps_cmd += "Write-Host 'Done'"
    
    # Escape for SSH (wrap in powershell -c)
    ok, stdout, stderr = _ssh_windows(f'powershell -c "{ps_cmd}"')
    if ok:
        return {"ok": True, "wsl_ip": wsl_ip}
    else:
        return {"ok": False, "error": stderr or "SSH command failed"}
