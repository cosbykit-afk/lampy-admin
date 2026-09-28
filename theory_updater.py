"""R Theory website updater: checks if the ledger/site has changed on GitHub
and deploys a new version of the site.

The pipeline is:
  theorem_ledger.md -> sync_status.py -> status_registry.json -> site build
  -> site-dist branch -> deployed to /var/www/r-theory

This module checks GitHub for changes and pulls the latest site-dist.
"""
import json
import os
import shutil
import tarfile
import tempfile
import urllib.request

REPO = "cosbykit-afk/r-theory-rewrite"
SITE_BRANCH = "site-dist"
WEB_ROOT = "/var/www/r-theory"
# Ledger files whose changes mean the site needs a rebuild
LEDGER_PATHS = ["status_registry.json", "ledger/index.html"]

def _github_api(path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{path}",
        headers={"User-Agent": "lampy-console-theory-updater",
                 "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)

def _get_setting(key, default=None):
    try:
        import settings
        return settings.get(key) or default
    except Exception:
        return default

def _set_setting(key, value):
    try:
        import settings
        settings.set(key, value)
    except Exception:
        pass

def check_theory_update():
    """Check GitHub for theory site changes.

    Returns dict with:
      ok, site_sha, site_changed, ledger_sha, ledger_changed,
      needs_rebuild (ledger changed but site-dist not yet rebuilt),
      ready_to_deploy (site-dist has new commits)
    """
    try:
        # Latest site-dist commit (deployable site)
        site_commit = _github_api(f"commits?sha={SITE_BRANCH}&per_page=1")
        site_sha = site_commit[0]["sha"][:8] if site_commit else None
        site_msg = site_commit[0]["commit"]["message"].split("\n")[0] if site_commit else ""
        site_date = site_commit[0]["commit"]["committer"]["date"] if site_commit else ""

        # Latest ledger change on main
        ledger_sha = None
        ledger_msg = ""
        ledger_date = ""
        for path in LEDGER_PATHS:
            commits = _github_api(f"commits?sha=main&path={path}&per_page=1")
            if commits:
                c = commits[0]
                # Take the most recent across ledger files
                if not ledger_date or c["commit"]["committer"]["date"] > ledger_date:
                    ledger_sha = c["sha"][:8]
                    ledger_msg = c["commit"]["message"].split("\n")[0]
                    ledger_date = c["commit"]["committer"]["date"]

        last_site = _get_setting("theory_site_sha")
        last_ledger = _get_setting("theory_ledger_sha")

        site_changed = site_sha and site_sha != last_site
        ledger_changed = ledger_sha and ledger_sha != last_ledger

        # If ledger changed but site-dist hasn't been rebuilt since,
        # the site is behind and needs a rebuild (done in the workspace)
        needs_rebuild = bool(ledger_changed and not site_changed)

        return {
            "ok": True,
            "site_sha": site_sha,
            "site_message": site_msg,
            "site_date": site_date,
            "site_changed": site_changed,
            "ledger_sha": ledger_sha,
            "ledger_message": ledger_msg,
            "ledger_date": ledger_date,
            "ledger_changed": ledger_changed,
            "needs_rebuild": needs_rebuild,
            "ready_to_deploy": bool(site_changed),
            "last_deployed": last_site,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

def apply_theory_update():
    """Download the latest site-dist and deploy to WEB_ROOT.

    Returns (ok, message).
    """
    info = check_theory_update()
    if not info.get("ok"):
        return False, f"Could not check for updates: {info.get('error')}"
    if not info.get("ready_to_deploy"):
        return False, "Site is already up to date."

    tarball_url = f"https://github.com/{REPO}/archive/refs/heads/{SITE_BRANCH}.tar.gz"

    try:
        tmpdir = tempfile.mkdtemp()
        tarball_path = os.path.join(tmpdir, "site.tar.gz")

        req = urllib.request.Request(tarball_url,
            headers={"User-Agent": "lampy-console-theory-updater"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(tarball_path, "wb") as f:
                shutil.copyfileobj(resp, f)

        with tarfile.open(tarball_path, "r:gz") as tar:
            tar.extractall(tmpdir)

        # Find extracted dir (r-theory-rewrite-site-dist)
        extracted = None
        for name in os.listdir(tmpdir):
            full = os.path.join(tmpdir, name)
            if os.path.isdir(full) and "r-theory-rewrite" in name:
                extracted = full
                break

        if not extracted:
            return False, "Could not find extracted site files."

        # Backup current site (keep 2)
        for i in range(2, 0, -1):
            old = f"{WEB_ROOT}.backup.{i}"
            new = f"{WEB_ROOT}.backup.{i+1}"
            if os.path.exists(old):
                if i == 2:
                    shutil.rmtree(old)
                else:
                    os.rename(old, new)
        if os.path.exists(WEB_ROOT):
            shutil.copytree(WEB_ROOT, f"{WEB_ROOT}.backup.1",
                          symlinks=True)

        # Deploy: replace contents of WEB_ROOT
        # (remove old, copy new — preserves the directory itself)
        for item in os.listdir(WEB_ROOT):
            path = os.path.join(WEB_ROOT, item)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        for item in os.listdir(extracted):
            src = os.path.join(extracted, item)
            dst = os.path.join(WEB_ROOT, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)

        # Fix ownership for Apache
        os.system(f"chown -R www-data:www-data {WEB_ROOT} 2>/dev/null")

        # Record deployed SHAs
        _set_setting("theory_site_sha", info["site_sha"])
        if info.get("ledger_sha"):
            _set_setting("theory_ledger_sha", info["ledger_sha"])

        shutil.rmtree(tmpdir)
        return True, f"Deployed site {info['site_sha']} ({info['site_message'][:60]})."
    except Exception as e:
        return False, f"Deploy failed: {e}"
