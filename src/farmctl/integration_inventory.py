"""Persistent integration switches, configuration snapshots, and event logs.

Records live beneath the existing farmctl archive root, so the current
whole-tree backup/restore commands protect them without a second backend.
Avoid putting credentials or private keys in configuration snapshots.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .device_inventory import default_log_root, ensure_log_path

COMPONENTS = (
    "slurm", "nats-jetstream", "ray", "cilium", "ansible", "nomad",
    "netbox", "kubernetes", "falco", "keda",
    "argocd", "tetragon", "tailscale", "temporal", "loki", "tempo",
    "deepspeed", "ipex-llm", "terraform", "kafka", "zeromq", "rabbitmq",
    "redpanda", "redisstream", "rubix", "apollo", "falcon",
    "victoriametrics", "graphql", "sql", "duckdb", "nubeio", "mqtt",
    "modbus", "nginx", "ngrok", "bacnet", "grafana", "boinc", "regex",
    "rsync", "rclone", "prometheus", "hetzner", "equinix-metal", "ovh",
    "ixp", "arin", "ripe", "truenas", "cloudflare", "wireguard",
    "son-of-grid-engine",
)


def _component_name(value: str) -> str:
    name = re.sub(r"[-\s_]+", "-", value.strip().lower())
    if name not in COMPONENTS:
        raise ValueError(f"Unknown component {value!r}; choose from: {', '.join(COMPONENTS)}")
    return name


def _json_object(value, label: str) -> dict:
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


class IntegrationStore:
    """Store per-component desired state and append-only event records."""

    def __init__(self, root: str | Path | None = None):
        self.root = ensure_log_path(Path(root).expanduser() if root else default_log_root()) / "integrations"
        self.root.mkdir(parents=True, exist_ok=True)

    def _directory(self, component: str) -> Path:
        return self.root / _component_name(component)

    def set_component(self, component: str, enabled: bool | None = None,
                      switches: dict | str | None = None,
                      config: dict | str | None = None) -> Path:
        name = _component_name(component)
        directory = self._directory(name)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "settings.json"
        previous = {}
        if path.exists():
            previous = json.loads(path.read_text())
        if enabled is not None:
            previous["enabled"] = bool(enabled)
        if switches is not None:
            previous["switches"] = _json_object(switches, "switches")
        if config is not None:
            previous["config"] = _json_object(config, "config")
        previous.update({"component": name, "updated_at": datetime.now(timezone.utc).isoformat()})
        path.write_text(json.dumps(previous, indent=2, sort_keys=True) + "\n")
        return path

    def log_event(self, component: str, event: str, data: dict | str | None = None) -> Path:
        name = _component_name(component)
        if not event or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,120}", event):
            raise ValueError("event must be 1-120 characters using letters, digits, . _ : / or -")
        directory = self._directory(name)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "events.jsonl"
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "component": name,
                  "event": event, "data": _json_object(data, "event data")}
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        return path

    def list_components(self) -> list[dict]:
        result = []
        for component in COMPONENTS:
            path = self._directory(component) / "settings.json"
            if path.exists():
                result.append(json.loads(path.read_text()))
        return result

    def get_component(self, component: str) -> dict | None:
        path = self._directory(component) / "settings.json"
        return json.loads(path.read_text()) if path.exists() else None

    def read_events(self, component: str, limit: int = 50) -> list[dict]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        path = self._directory(component) / "events.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in lines]
