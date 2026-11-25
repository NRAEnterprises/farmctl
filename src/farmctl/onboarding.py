"""Single-command node onboarding across explicitly configured CLI adapters.

Adapters are argv arrays configured per component. They are executed without
shell expansion, only on an explicit --apply, and run sequentially so results
can be recorded per tool. farmctl does not assume credentials or cluster
context for any third-party system.
"""

import shutil
import subprocess
import time
import json
import re
from datetime import datetime, timezone

from . import device_inventory as di
from .integration_inventory import COMPONENTS, IntegrationStore

PLACEHOLDERS = ("hostname", "device_id", "manufacturer", "model", "device_type",
                "architecture", "kernel_release", "cpu_threads", "memory_mb",
                "inventory_json", "loki_push_json")


def _expand_argv(argv, values):
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and arg for arg in argv):
        raise ValueError("onboard_argv must be a non-empty array of strings")
    expanded = []
    for arg in argv:
        for key in PLACEHOLDERS:
            arg = arg.replace("{" + key + "}", str(values.get(key, "") or ""))
        expanded.append(arg)
    return expanded


def build_onboarding_plan(profile, integrations: IntegrationStore):
    device_id = di._device_id(profile)
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
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }, separators=(",", ":"), sort_keys=True)
    loki_line = json.dumps({
        "event": "farmctl.node.onboarding",
        "inventory": json.loads(values["inventory_json"]),
    }, separators=(",", ":"), sort_keys=True)
    values["loki_push_json"] = json.dumps({
        "streams": [{
            "stream": {"job": "farmctl-node", "host": profile.hostname},
            "values": [[str(time.time_ns()), loki_line]],
        }],
    }, separators=(",", ":"), sort_keys=True)
    steps = []
    for component in COMPONENTS:
        settings = integrations.get_component(component)
        if not settings or settings.get("enabled") is not True:
            steps.append({"component": component, "status": "disabled_or_unconfigured"})
            continue
        config = settings.get("config") or {}
        argv = config.get("onboard_argv")
        if not argv:
            steps.append({"component": component, "status": "enabled_but_no_adapter"})
            continue
        try:
            expanded = _expand_argv(argv, values)
        except ValueError as exc:
            steps.append({"component": component, "status": "invalid_adapter", "detail": str(exc)})
            continue
        raw_preflight = config.get("preflight_argv")
        if not raw_preflight:
            preflight = None
            status = "preflight_missing"
        else:
            try:
                preflight = _expand_argv(raw_preflight, values)
            except ValueError as exc:
                steps.append({"component": component, "status": "invalid_adapter", "detail": str(exc)})
                continue
            required = set()
            for arg in list(argv) + list(raw_preflight):
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
                status = "preflight_executable_not_found"
            elif not shutil.which(expanded[0]):
                status = "executable_not_found"
            else:
                status = "ready"
        steps.append({
            "component": component,
            "status": status,
            "executable": expanded[0],
            "argv": expanded,
            "preflight_argv": preflight,
        })
    return device_id, values, steps


def validate_adapters(profile, integrations: IntegrationStore, timeout: float = 30.0,
                      components: list[str] | None = None):
    """Run only configured preflight argv arrays; they must be read-only probes."""
    device_id, values, _ = build_onboarding_plan(profile, integrations)
    selected = set(components) if components is not None else set(COMPONENTS)
    results = []
    for component in COMPONENTS:
        if component not in selected:
            continue
        settings = integrations.get_component(component)
        if not settings or settings.get("enabled") is not True:
            results.append({"component": component,
                            "status": "not_configured" if components is not None else "disabled_or_unconfigured"})
            continue
        config = settings.get("config") or {}
        raw_preflight = config.get("preflight_argv")
        if not raw_preflight:
            results.append({"component": component, "status": "preflight_missing"})
            continue
        try:
            preflight = _expand_argv(raw_preflight, values)
        except ValueError as exc:
            results.append({"component": component, "status": "invalid_preflight", "detail": str(exc)})
            continue
        required = set()
        for arg in raw_preflight:
            required.update(re.findall(r"\{([^{}]+)\}", arg))
        unknown = sorted(required - set(PLACEHOLDERS))
        missing = sorted(key for key in required if key in values and not values[key])
        if unknown:
            results.append({"component": component, "status": "invalid_preflight",
                            "detail": "unknown placeholders: " + ", ".join(unknown)})
            continue
        if missing:
            results.append({"component": component, "status": "missing_profile_fields",
                            "detail": ", ".join(missing)})
            continue
        if not shutil.which(preflight[0]):
            results.append({"component": component, "status": "preflight_executable_not_found"})
            continue
        onboard = config.get("onboard_argv")
        if onboard:
            try:
                onboard_expanded = _expand_argv(onboard, values)
            except ValueError as exc:
                results.append({"component": component, "status": "invalid_adapter", "detail": str(exc)})
                continue
            if not shutil.which(onboard_expanded[0]):
                results.append({"component": component, "status": "executable_not_found"})
                continue
        started = time.monotonic()
        try:
            proc = subprocess.run(preflight, capture_output=True, text=True,
                                  timeout=timeout, check=False)
            results.append({"component": component,
                            "status": "passed" if proc.returncode == 0 else "failed",
                            "returncode": proc.returncode,
                            "duration_seconds": round(time.monotonic() - started, 3)})
        except subprocess.TimeoutExpired:
            results.append({"component": component, "status": "timed_out",
                            "duration_seconds": round(time.monotonic() - started, 3)})
        except OSError as exc:
            results.append({"component": component, "status": "failed", "detail": str(exc)})
    return {"device_id": device_id, "steps": results,
            "passed": all(item["status"] in ("passed", "disabled_or_unconfigured") for item in results)}


def onboard_node(target: str, connection_type: str, apply: bool = False,
                 timeout: float = 120.0, store=None, integrations=None):
    """Fingerprint and optionally apply all configured adapters in order."""
    store = store or di.DeviceLogStore()
    integrations = integrations or IntegrationStore(root=store.root)
    profile = di.fingerprint_device(target, connection_type)
    if profile.os_family == "unknown":
        raise RuntimeError(f"Could not identify {target!r} over {connection_type}; no actions were run")

    device_id, values, plan = build_onboarding_plan(profile, integrations)
    if not apply:
        return {"applied": False, "device_id": device_id, "profile": di._to_serializable(profile), "steps": plan}

    blocked = [step for step in plan if step["status"] not in ("disabled_or_unconfigured", "ready")]
    if blocked:
        summary = ", ".join(f"{item['component']}={item['status']}" for item in blocked)
        raise RuntimeError(f"Adapter preflight/configuration requirements not met: {summary}")

    preflight = validate_adapters(profile, integrations, timeout=min(timeout, 30.0))
    if not preflight["passed"]:
        summary = ", ".join(f"{item['component']}={item['status']}" for item in preflight["steps"]
                             if item["status"] not in ("passed", "disabled_or_unconfigured"))
        raise RuntimeError(f"Preflight failed; no onboarding adapters were run: {summary}")

    snapshot = store.write_snapshot(profile)
    results = []
    for step in plan:
        component = step["component"]
        if step["status"] != "ready":
            result = {key: value for key, value in step.items() if key != "argv"}
        else:
            started = time.monotonic()
            try:
                proc = subprocess.run(step["argv"], capture_output=True, text=True,
                                     timeout=timeout, check=False)
                result = {"component": component,
                          "status": "succeeded" if proc.returncode == 0 else "failed",
                          "returncode": proc.returncode,
                          "duration_seconds": round(time.monotonic() - started, 3)}
            except subprocess.TimeoutExpired:
                result = {"component": component, "status": "timed_out",
                          "duration_seconds": round(time.monotonic() - started, 3)}
            except OSError as exc:
                result = {"component": component, "status": "failed", "detail": str(exc)}
        integrations.log_event(component, "node.onboarding", {
            "device_id": device_id, "hostname": profile.hostname,
            "status": result["status"],
        })
        results.append(result)

    return {"applied": True, "device_id": device_id, "snapshot": str(snapshot),
            "profile": di._to_serializable(profile), "steps": results}
