"""Small, independent user preferences; library paths stay in config.yaml."""
import json
from . import config

# beta: experimental features (today: the Add-ons tab) -- off until the
# user turns them on in Settings, after being told they are experimental.
DEFAULTS = {"auto_scan": True, "scan_interval": 60, "covers": True, "beta": False}


def load():
    try:
        return {**DEFAULTS, **json.loads((config.DATA_DIR / "preferences.json").read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def _write(values: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DATA_DIR / "preferences.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(values), encoding="utf-8")
    temporary.replace(path)


def save(auto_scan: bool, scan_interval: int, covers: bool):
    if not 10 <= scan_interval <= 3600:
        raise ValueError("Scan interval must be between 10 and 3600 seconds")
    values = dict(auto_scan=auto_scan, scan_interval=scan_interval, covers=covers)
    # Whatever else is saved (beta) stays as it was.
    _write({**load(), **values})
    return values


def beta_enabled() -> bool:
    return bool(load().get("beta"))


def set_beta(enabled: bool) -> bool:
    _write({**load(), "beta": bool(enabled)})
    return bool(enabled)
