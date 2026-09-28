"""Self-updater: checks GitHub for newer releases and applies them."""
import json
import os
import shutil
import tarfile
import tempfile
import urllib.request

REPO = "cosbykit-afk/lampy-admin"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
INSTALL_DIR = "/opt/lampy-console"

def get_current_version():
    """Read version from settings."""
    try:
        import settings
        return settings.get("version") or "1.1.0"
    except Exception:
        return "1.1.0"

def check_for_update():
    """Query GitHub for the latest release. Returns dict with update info."""
    try:
        req = urllib.request.Request(
            API_URL,
            headers={"User-Agent": "lampy-console-updater", "Accept": "application/vnd.github+json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        
        latest_tag = data.get("tag_name", "").lstrip("v")
        current = get_current_version().lstrip("v")
        
        # Simple version comparison
        def parse_ver(v):
            return [int(x) for x in v.split(".") if x.isdigit()]
        
        has_update = parse_ver(latest_tag) > parse_ver(current)
        
        return {
            "ok": True,
            "current": current,
            "latest": latest_tag,
            "has_update": has_update,
            "release_name": data.get("name", ""),
            "release_notes": data.get("body", "")[:2000],
            "published_at": data.get("published_at", ""),
            "html_url": data.get("html_url", ""),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

def apply_update():
    """Download and apply the latest release. Returns (ok, message)."""
    info = check_for_update()
    if not info.get("ok"):
        return False, f"Could not check for updates: {info.get('error')}"
    if not info.get("has_update"):
        return False, "Already on the latest version."
    
    tag = "v" + info["latest"]
    tarball_url = f"https://github.com/{REPO}/archive/refs/tags/{tag}.tar.gz"
    
    try:
        # Download to temp
        tmpdir = tempfile.mkdtemp()
        tarball_path = os.path.join(tmpdir, "update.tar.gz")
        
        req = urllib.request.Request(tarball_url, headers={"User-Agent": "lampy-console-updater"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            with open(tarball_path, "wb") as f:
                shutil.copyfileobj(resp, f)
        
        # Extract
        with tarfile.open(tarball_path, "r:gz") as tar:
            tar.extractall(tmpdir)
        
        # Find extracted dir (lampy-admin-<tag>)
        extracted = None
        for name in os.listdir(tmpdir):
            full = os.path.join(tmpdir, name)
            if os.path.isdir(full) and name.startswith("lampy-admin-"):
                extracted = full
                break
        
        if not extracted:
            return False, "Could not find extracted files."
        
        # Backup current (keep last 3)
        backup_base = INSTALL_DIR + ".backup"
        for i in range(3, 0, -1):
            old = f"{backup_base}.{i}"
            new = f"{backup_base}.{i+1}"
            if os.path.exists(old):
                if i == 3:
                    shutil.rmtree(old)
                else:
                    os.rename(old, new)
        if os.path.exists(INSTALL_DIR):
            shutil.copytree(INSTALL_DIR, f"{backup_base}.1")
        
        # Copy new files over (preserve settings.json and local config)
        preserve = {"settings.json"}
        for item in os.listdir(extracted):
            src = os.path.join(extracted, item)
            dst = os.path.join(INSTALL_DIR, item)
            if item in preserve and os.path.exists(dst):
                continue
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        
        # Update version in settings
        try:
            import settings
            settings.set("version", info["latest"])
        except Exception:
            pass
        
        # Cleanup
        shutil.rmtree(tmpdir)
        
        return True, f"Updated to v{info['latest']}. Restart the console to apply."
    except Exception as e:
        return False, f"Update failed: {e}"
