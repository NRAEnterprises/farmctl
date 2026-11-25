"""Plan-first, explicit node teardown across configured integration adapters.

External systems have incompatible lifecycle semantics. farmctl therefore runs
only operator-supplied teardown argv arrays for tools that have an onboarding
action recorded for this device; it never guesses a deletion command.
"""

import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import device_inventory as di
from .integration_inventory import COMPONENTS, IntegrationStore
from .onboarding import PLACEHOLDERS, _expand_argv

ATTEMPTED_STATUSES = {"succeeded", "failed", "timed_out"}


def _profile_values(profile, device_id):
    values = {
        "hostname": profile.hostname,
        "device_id": device_id,
        "manufacturer": profile.manufacturer,
        "model": profile.model,
        "device_type": profile.device_type,
        "architecture": profile.architecture,
        "kernel_release": profile.kernel_release,
        "cpu_threads": profile.cpu_threads,
        "memory_mb": profile.memory_mb,
    }
    values["inventory_json"] = json.dumps({
        "device_id": device_id,
        "hostname": profile.hostname,
        "manufacturer": profile.manufacturer,
        "model": profile.model,
        "device_type": profile.device_type,
        "architecture": profile.architecture,
        "kernel_release": profile.kernel_release,
        "cpu_threads": profile.cpu_threads,
        "memory_mb": profile.memory_mb,
    }, separators=(",", ":"), sort_keys=True)
    values["loki_push_json"] = ""
    return values


def _node_steps(device_id, integrations, values):
    steps = []
    for component in COMPONENTS:
        settings = integrations.get_component(component)
        events = integrations.read_events(component, limit=100000)
        node_events = [
            event for event in events
            if event.get("event") == "node.onboarding"
            and event.get("data", {}).get("device_id") == device_id
        ]
        if not any(event.get("data", {}).get("status") in ATTEMPTED_STATUSES
                   for event in node_events):
            continue

        config = (settings or {}).get("config") or {}
        raw_teardown = config.get("teardown_argv")
        raw_preflight = config.get("preflight_argv")
        if not raw_teardown:
            steps.append({"component": component, "status": "teardown_missing"})
            continue
        if not raw_preflight:
            steps.append({"component": component, "status": "preflight_missing"})
            continue
        try:
            teardown = _expand_argv(raw_teardown, values)
            preflight = _expand_argv(raw_preflight, values)
        except ValueError as exc:
            steps.append({"component": component, "status": "invalid_adapter",
                          "detail": str(exc)})
            continue

        required = set()
        for arg in list(raw_teardown) + list(raw_preflight):
            required.update(re.findall(r"\{([^{}]+)\}", arg))
        unknown = sorted(required - set(PLACEHOLDERS))
        if unknown:
            steps.append({"component": component, "status": "invalid_adapter",
                          "detail": "unknown placeholders: " + ", ".join(unknown)})
            continue
        missing = sorted(key for key in required if key in values and not values[key])
        if missing:
            steps.append({"component": component, "status": "missing_profile_fields",
                          "detail": ", ".join(missing)})
            continue
        if not shutil.which(preflight[0]):
            steps.append({"component": component,
                          "status": "preflight_executable_not_found"})
            continue
        if not shutil.which(teardown[0]):
            steps.append({"component": component,
                          "status": "teardown_executable_not_found"})
            continue
        steps.append({"component": component, "status": "ready",
                      "preflight_argv": preflight, "teardown_argv": teardown})
    return steps


def purge_node(target: str, connection_type: str, *, apply: bool = False,
               confirm_device_id: str | None = None, timeout: float = 120.0,
               store=None, integrations=None):
    """Build a teardown plan, or apply explicit reversals and purge local snapshot.

    The operator must first review a plan, then repeat with the exact device ID.
    Integration events/configuration remain as audit/history; only the selected
    device's snapshot directory is removed after every teardown succeeds.
    """
    store = store or di.DeviceLogStore()
    integrations = integrations or IntegrationStore(root=store.root)
    profile = di.fingerprint_device(target, connection_type)
    if profile.os_family == "unknown":
        raise RuntimeError(
            f"Could not identify {target!r} over {connection_type}; no actions were run"
        )

    device_id = di._device_id(profile)
    values = _profile_values(profile, device_id)
    steps = _node_steps(device_id, integrations, values)
    result = {"applied": False, "device_id": device_id,
              "profile": di._to_serializable(profile), "steps": steps}
    if not apply:
        return result
    if confirm_device_id != device_id:
        raise RuntimeError(
            f"Confirmation mismatch; rerun with --confirm-device-id {device_id}"
        )

    blocked = [step for step in steps if step["status"] != "ready"]
    if blocked:
        summary = ", ".join(
            f"{step['component']}={step['status']}" for step in blocked
        )
        raise RuntimeError(f"Purge blocked before external actions: {summary}")

    started_at = datetime.now(timezone.utc).isoformat()
    # Complete every service preflight before running any external teardown.
    for step in steps:
        try:
            proc = subprocess.run(
                step["preflight_argv"], capture_output=True, text=True,
                timeout=min(timeout, 30.0), check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            preflight_failure = [{
                "component": step["component"], "status": "failed",
                "detail": str(exc), "phase": "preflight",
            }]
            _write_audit(integrations.root, device_id, target, started_at,
                         preflight_failure, local_snapshot_removed=False)
            raise RuntimeError(
                f"Purge preflight failed for {step['component']}: {exc}"
            ) from exc
        if proc.returncode != 0:
            preflight_failure = [{
                "component": step["component"], "status": "failed",
                "detail": f"exit {proc.returncode}", "phase": "preflight",
            }]
            _write_audit(integrations.root, device_id, target, started_at,
                         preflight_failure, local_snapshot_removed=False)
            raise RuntimeError(
                f"Purge preflight failed for {step['component']} "
                f"(exit {proc.returncode}); no teardowns were run"
            )

    results = []
    for step in steps:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                step["teardown_argv"], capture_output=True, text=True,
                timeout=timeout, check=False,
            )
            status = "succeeded" if proc.returncode == 0 else "failed"
            detail = None if proc.returncode == 0 else f"exit {proc.returncode}"
        except subprocess.TimeoutExpired:
            status, detail = "timed_out", f"timeout after {timeout}s"
        except OSError as exc:
            status, detail = "failed", str(exc)

        result_step = {
            "component": step["component"], "status": status,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        if detail:
            result_step["detail"] = detail
        results.append(result_step)
        if status != "succeeded":
            _write_audit(integrations.root, device_id, target, started_at, results,
                         local_snapshot_removed=False)
            raise RuntimeError(
                f"Teardown failed for {step['component']}; earlier external "
                "teardowns may have succeeded; local snapshot retained"
            )
        integrations.log_event(step["component"], "node.purged", {
            "device_id": device_id, "hostname": profile.hostname, "status": status,
        })

    # Guard against path traversal and remove only the canonical device-ID directory.
    if not re.fullmatch(r"[0-9a-f]{16}", device_id):
        raise RuntimeError("Refusing to remove local data: invalid device ID")
    device_dir = (Path(store.root) / device_id).resolve()
    root = Path(store.root).resolve()
    if device_dir.parent != root:
        raise RuntimeError("Refusing to remove local data outside the archive root")
    try:
        if device_dir.exists():
            shutil.rmtree(device_dir, ignore_errors=False)
    except OSError as exc:
        _write_audit(integrations.root, device_id, target, started_at, results,
                     local_snapshot_removed=False)
        raise RuntimeError(
            f"External teardowns succeeded but local snapshot removal failed: {exc}"
        ) from exc

    _write_audit(integrations.root, device_id, target, started_at, results,
                 local_snapshot_removed=True)
    result.update({"applied": True, "steps": results,
                   "local_snapshot_removed": True})
    return result


def _write_audit(integration_root, device_id, target, started_at, steps,
                 local_snapshot_removed):
    path = Path(integration_root) / "purge_audit.jsonl"
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "started_at": started_at,
        "device_id": device_id,
        "target": target,
        "steps": steps,
        "local_snapshot_removed": local_snapshot_removed,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
