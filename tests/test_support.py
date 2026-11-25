"""Minimal optional-prerequisite stubs so tests do not touch real services."""
import sys
import types
from pathlib import Path


def _ensure(path, **kwargs):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_chain(path, *parts):
    result = Path(path).joinpath(*parts)
    result.mkdir(parents=True, exist_ok=True)
    return result


def _sanitize(value):
    return str(value).strip().replace("/", "-") or "unknown"


def _unique(path, name, extension=""):
    result = Path(path) / f"{name}{extension}"
    index = 1
    while result.exists():
        result = Path(path) / f"{name}-{index}{extension}"
        index += 1
    return result


if "autopath" not in sys.modules:
    autopath = types.ModuleType("autopath")
    autopath.ensure_path = _ensure
    autopath.ensure_path_chain = _ensure_chain
    autopath.sanitize_component = _sanitize
    autopath.resolve_unique_path = _unique
    sys.modules["autopath"] = autopath

if "cloud_restore" not in sys.modules:
    cloud_restore = types.ModuleType("cloud_restore")
    for name in ("print_sync_result", "restore_or_raise", "backup_project", "restore_project",
                 "backup_project_compressed", "restore_project_compressed", "restore_project_auto"):
        setattr(cloud_restore, name, lambda *args, **kwargs: None)
    compression = types.ModuleType("cloud_restore.compression")
    compression.detect_available_compressors = lambda: {}
    compression.recommend_best_available = lambda: "gzip"
    cloud_restore.compression = compression
    sys.modules["cloud_restore"] = cloud_restore
    sys.modules["cloud_restore.compression"] = compression
