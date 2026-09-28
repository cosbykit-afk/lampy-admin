"""Console settings storage (JSON file, root-owned)."""
import json
import os

SETTINGS_FILE = "/opt/lampy-console/settings.json"

DEFAULTS = {
    "forum_registered_only": False,
    "version": "1.1.0",
}

def _load():
    if not os.path.exists(SETTINGS_FILE):
        return dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        # Merge with defaults for any missing keys
        for k, v in DEFAULTS.items():
            if k not in data:
                data[k] = v
        return data
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)

def _save(data):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(SETTINGS_FILE, 0o600)

def get(key):
    return _load().get(key, DEFAULTS.get(key))

def set(key, value):
    data = _load()
    data[key] = value
    _save(data)

def all_settings():
    return _load()
