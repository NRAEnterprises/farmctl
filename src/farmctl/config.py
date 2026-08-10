"""
Small persistent config for farmctl, so backend/remote/project settings
only need to be entered once (via `farmctl configure`) instead of being
passed as flags on every single command. This is what makes the backup/
restore loop actually seamless rather than "a ton of manual work for
every single file" — one configure step, then every scan/backup/restore
call picks the same settings up automatically.

Stored per-user at ~/.config/farmctl/config.json (this is genuinely
per-user config, not the device-log data itself, so unlike the log
storage path it's fine for this to live under the invoking user's home
directory on both Linux and Termux).
"""

import json
from pathlib import Path

from autopath import ensure_path

CONFIG_DIR = Path.home() / ".config" / "farmctl"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULTS = {
    "backend": None,          # "rsync" or "rclone"
    "rsync_remote_root": None,
    "rclone_remote_name": None,
    "rclone_remote_root": "",
    "project_name": "device-inventory",
    "compress": False,        # whether backup/restore compress by default
    "compress_algorithm": None,  # None = auto-pick strongest installed
    "compress_level": None,
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def save_config(config: dict) -> Path:
    ensure_path(CONFIG_DIR)
    CONFIG_PATH.write_text(json.dumps(config, indent=2, sort_keys=True))
    return CONFIG_PATH


def build_backend_from_config(config: dict):
    """Returns a ready-to-use SyncBackend instance from stored config,
    or None if nothing's configured yet — callers should treat None as
    'run `farmctl configure` first', not silently proceed."""
    from cloud_restore import RsyncBackend, RcloneBackend

    if config.get("backend") == "rsync" and config.get("rsync_remote_root"):
        return RsyncBackend(remote_root=config["rsync_remote_root"])
    if config.get("backend") == "rclone" and config.get("rclone_remote_name"):
        return RcloneBackend(
            remote_name=config["rclone_remote_name"],
            remote_root=config.get("rclone_remote_root", ""),
        )
    return None
