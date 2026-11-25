"""
farmctl — one CLI tying together device fingerprinting/archiving
(device_inventory) and cloud backup/restore (cloud_restore), so the
files device_inventory writes are exactly what backup/restore already
knows how to sync as a whole tree — no per-file registration, no manual
step between "device integrated" and "device backed up."

Typical flow:
    farmctl configure                     # once
    farmctl scan node-1.local --type ssh
    farmctl scan emulator-5554 --type adb
    farmctl backup                        # syncs the whole local archive
    ...later, after a rebuild...
    farmctl restore
    farmctl rebuild
"""

import sys
import argparse
import json
import curses

from pathlib import Path

from .config import load_config, save_config, build_backend_from_config
from . import device_inventory as di
from .integration_inventory import IntegrationStore, COMPONENTS
from .onboarding import onboard_node, validate_adapters
from .purge import purge_node
from .integration_menu import run_integrations_menu
from cloud_restore import (
    print_sync_result, restore_or_raise, backup_project, restore_project,
    backup_project_compressed, restore_project_compressed, restore_project_auto,
)
from cloud_restore.compression import detect_available_compressors, recommend_best_available

# autopath's own registry (registered aliases, migrations log) is
# per-user local state living at a fixed path autopath itself owns —
# see ~/.config/autopath/paths.json. Backing it up is just "sync this
# directory like any other," via cloud_restore's already-generic
# functions — this does NOT create a code dependency between autopath
# and cloud_restore; the composition lives entirely here, in farmctl,
# which is the layer explicitly allowed to know about both. Neither
# core package needs to change or know this happens.
AUTOPATH_CONFIG_DIR = Path.home() / ".config" / "autopath"
AUTOPATH_REGISTRY_PROJECT = "autopath-registry"


def _sync_autopath_registry(backend, direction: str, dry_run: bool = False) -> None:
    """
    Best-effort secondary sync of autopath's local registry, run
    alongside the main device-archive backup/restore. Failures here are
    reported but never turn a successful device-archive operation into
    a failed command — the registry is a convenience layer (skip
    re-registering aliases after a rebuild), not the primary data this
    tool exists to protect.
    """
    if direction == "backup":
        if not AUTOPATH_CONFIG_DIR.exists():
            return  # nothing registered yet on this machine -- fine, nothing to back up
        result = backup_project(backend, AUTOPATH_REGISTRY_PROJECT, AUTOPATH_CONFIG_DIR, dry_run=dry_run)
    else:
        result = restore_project_auto(backend, AUTOPATH_REGISTRY_PROJECT, AUTOPATH_CONFIG_DIR, dry_run=dry_run)

    label = "backup" if direction == "backup" else "restore"
    if result.success:
        print(f"(autopath registry {label}: OK)")
    else:
        print(f"(autopath registry {label} skipped/failed — device archive {label} is unaffected: {result.stderr})",
              file=sys.stderr)


def cmd_configure(args) -> int:
    config = load_config()
    print("farmctl configuration — press Enter to keep the current value shown in [brackets].\n")

    backend = input(f"Backend, rsync or rclone [{config.get('backend') or 'none set'}]: ").strip().lower()
    if backend:
        config["backend"] = backend

    if config.get("backend") == "rsync":
        remote_root = input(
            f"rsync remote root, e.g. user@host:/path or /mnt/nas/backups "
            f"[{config.get('rsync_remote_root') or 'none set'}]: "
        ).strip()
        if remote_root:
            config["rsync_remote_root"] = remote_root

    elif config.get("backend") == "rclone":
        remote_name = input(
            f"rclone remote name (from `rclone config`) [{config.get('rclone_remote_name') or 'none set'}]: "
        ).strip()
        if remote_name:
            config["rclone_remote_name"] = remote_name
        remote_root = input(
            f"rclone remote root path/bucket prefix (optional) [{config.get('rclone_remote_root') or ''}]: "
        ).strip()
        if remote_root:
            config["rclone_remote_root"] = remote_root

    project_name = input(
        f"Project name (namespaces this data on the remote) [{config.get('project_name')}]: "
    ).strip()
    if project_name:
        config["project_name"] = project_name

    compress = input(
        f"Compress backups by default? y/n [{'y' if config.get('compress') else 'n'}]: "
    ).strip().lower()
    if compress:
        config["compress"] = compress in ("y", "yes")

    if config.get("compress"):
        avail = detect_available_compressors()
        installed = ", ".join(name for name, ok in sorted(avail.items()) if ok)
        print(f"(available compressors on this machine: {installed})")
        algo = input(
            f"Compression algorithm, or blank to auto-pick the strongest installed "
            f"[{config.get('compress_algorithm') or 'auto'}]: "
        ).strip()
        if algo:
            config["compress_algorithm"] = None if algo == "auto" else algo
        level = input(
            f"Compression level, or blank for the algorithm's default "
            f"[{config.get('compress_level') if config.get('compress_level') is not None else 'default'}]: "
        ).strip()
        if level:
            try:
                config["compress_level"] = int(level)
            except ValueError:
                print("(not a number — leaving level at its previous value)")

    path = save_config(config)
    print(f"\nSaved to {path}")
    return 0


def cmd_scan(args) -> int:
    profile = di.fingerprint_device(args.target, args.type)
    if profile.os_family == "unknown":
        print(
            f"Could not reach or identify '{args.target}' over {args.type} — "
            f"nothing was archived. Check connectivity/auth and try again.",
            file=sys.stderr,
        )
        return 1
    di.write_taxonomy_log(profile)
    return 0


def cmd_backup(args) -> int:
    config = load_config()
    backend = build_backend_from_config(config)
    if backend is None:
        print("No backend configured. Run `farmctl configure` first.", file=sys.stderr)
        return 1

    project = args.project or config["project_name"]
    local_path = di.default_log_root()

    use_compress = args.compress if args.compress is not None else config.get("compress", False)
    if use_compress:
        algorithm = args.algorithm or config.get("compress_algorithm")
        level = args.level if args.level is not None else config.get("compress_level")
        result = backup_project_compressed(backend, project, local_path,
                                            algorithm=algorithm, level=level, dry_run=args.dry_run)
    else:
        result = backup_project(backend, project, local_path, mirror=args.mirror, dry_run=args.dry_run)

    print_sync_result(result)

    if not args.no_autopath_registry:
        _sync_autopath_registry(backend, "backup", dry_run=args.dry_run)

    return 0 if result.success else 1


def cmd_restore(args) -> int:
    config = load_config()
    backend = build_backend_from_config(config)
    if backend is None:
        print("No backend configured. Run `farmctl configure` first.", file=sys.stderr)
        return 1

    project = args.project or config["project_name"]
    local_path = di.default_log_root()

    # restore_project_auto() checks the remote's own manifest to find
    # out whether this backup was made compressed or not (and with
    # which algorithm) — no need to track or pass that here at all,
    # even if compression settings changed between backup runs.
    if args.fail_loud:
        result = restore_project_auto(backend, project, local_path, dry_run=args.dry_run)
        if not result.success:
            print(f"RESTORE FAILED: {result.stderr}", file=sys.stderr)
            return 1
    else:
        result = restore_project_auto(backend, project, local_path, dry_run=args.dry_run)

    print_sync_result(result)

    if not args.no_autopath_registry:
        _sync_autopath_registry(backend, "restore", dry_run=args.dry_run)

    return 0 if result.success else 1


def cmd_compressors(args) -> int:
    avail = detect_available_compressors()
    best = recommend_best_available()
    for name, ok in sorted(avail.items()):
        builtin = " (stdlib, always available)" if name in ("gzip", "bzip2", "xz") else ""
        marker = "installed" if ok else "NOT installed"
        print(f"{name:8} {marker}{builtin}")
    print(f"\nauto-selected best available: {best}")
    return 0


def cmd_rebuild(args) -> int:
    store = di.DeviceLogStore()
    di.rebuild_from_logs(store)
    return 0


def cmd_merge(args) -> int:
    store = di.DeviceLogStore()
    di.restore_from_cloud_sync(args.source_dirs, destination_store=store, dry_run=args.dry_run)
    return 0


def cmd_status(args) -> int:
    config = load_config()
    print("--- farmctl config ---")
    for k, v in config.items():
        print(f"  {k}: {v}")
    print()
    store = di.DeviceLogStore()
    print(f"Local archive root: {store.root}")
    print(f"Devices tracked:    {len(store.list_devices())}")
    return 0


def cmd_integration_set(args) -> int:
    store = IntegrationStore()
    enabled = True if args.enable else False if args.disable else None
    try:
        path = store.set_component(args.component, enabled=enabled,
                                   switches=args.switches_json, config=args.config_json)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid integration data: {exc}", file=sys.stderr)
        return 2
    print(f"Integration settings saved: {path}")
    return 0


def cmd_integration_show(args) -> int:
    store = IntegrationStore()
    try:
        if args.component:
            records = [store.get_component(args.component)]
        else:
            records = store.list_components()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps([record for record in records if record is not None], indent=2, sort_keys=True))
    return 0


def cmd_integration_log(args) -> int:
    try:
        path = IntegrationStore().log_event(args.component, args.event, args.data_json)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid event data: {exc}", file=sys.stderr)
        return 2
    print(f"Event appended: {path}")
    return 0


def cmd_integration_events(args) -> int:
    try:
        events = IntegrationStore().read_events(args.component, args.limit)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(events, indent=2, sort_keys=True))
    return 0


def cmd_integrate(args) -> int:
    overrides = {component: True for component in args.enable_component}
    for component in args.skip_component:
        if component in overrides:
            print(f"Component {component!r} cannot be both enabled and skipped for one run.",
                  file=sys.stderr)
            return 2
        overrides[component] = False
    try:
        result = onboard_node(args.target, args.type, apply=args.apply,
                              timeout=args.timeout, component_overrides=overrides)
    except (RuntimeError, ValueError) as exc:
        print(f"Onboarding stopped: {exc}", file=sys.stderr)
        return 1
    print(f"Node: {result['profile'].get('hostname')}  ID: {result['device_id']}")
    if result.get("snapshot"):
        print(f"Inventory snapshot: {result['snapshot']}")
    print("Mode: APPLY" if args.apply else "Mode: PLAN ONLY (no changes made; pass --apply to execute)")
    failed = False
    for step in result["steps"]:
        status = step["status"]
        detail = f" ({step['detail']})" if step.get("detail") else ""
        executable = f" via {step['executable']}" if step.get("executable") else ""
        print(f"  {step['component']}: {status}{executable}{detail}")
        failed = failed or status in ("failed", "timed_out", "executable_not_found",
                                      "preflight_missing", "preflight_executable_not_found",
                                      "enabled_but_no_adapter", "invalid_adapter",
                                      "missing_profile_fields")
    if not args.apply:
        ready_count = sum(1 for step in result["steps"] if step["status"] == "ready")
        print(f"Ready adapters: {ready_count}; configure enabled tools with integration set <component> --enable --config-json '{{\"onboard_argv\":[...]}}'")
    return 1 if failed else 0


def cmd_validate_adapters(args) -> int:
    profile = di.fingerprint_device(args.target, args.type)
    if profile.os_family == "unknown":
        print(f"Could not identify {args.target!r}; no preflight probes were run.", file=sys.stderr)
        return 1
    result = validate_adapters(profile, IntegrationStore(), timeout=args.timeout,
                               components=args.component)
    print(f"Read-only adapter preflight for {profile.hostname} ({result['device_id']})")
    failed = False
    for step in result["steps"]:
        details = f" ({step['detail']})" if step.get("detail") else ""
        print(f"  {step['component']}: {step['status']}{details}")
        if step["status"] not in ("passed", "disabled_or_unconfigured"):
            failed = True
    return 1 if failed else 0


def cmd_integrations(args) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("The integrations selector requires an interactive terminal.", file=sys.stderr)
        return 2
    try:
        result = run_integrations_menu()
    except curses.error as exc:
        print(f"Could not start the terminal selector: {exc}", file=sys.stderr)
        return 2
    if not result["saved"]:
        print("Integration selection cancelled; no settings changed.")
        return 0
    active = len(result["selected"])
    missing = sum(not tool.available for tool in result["presence"].values())
    print(f"Integration defaults saved: {active} active; {missing} unavailable and disabled.")
    return 0


def cmd_purge(args) -> int:
    try:
        result = purge_node(args.target, args.type, apply=args.apply,
                            confirm_device_id=args.confirm_device_id,
                            timeout=args.timeout)
    except (RuntimeError, ValueError) as exc:
        print(f"Purge stopped: {exc}", file=sys.stderr)
        return 1
    print(f"Node: {result['profile'].get('hostname')}  ID: {result['device_id']}")
    print("Mode: APPLY" if args.apply else "Mode: PLAN ONLY (no changes made)")
    for step in result["steps"]:
        print(f"  {step['component']}: {step['status']}")
        if step.get("preflight_argv"):
            print("    preflight: " + json.dumps(step["preflight_argv"]))
        if step.get("teardown_argv"):
            print("    teardown:  " + json.dumps(step["teardown_argv"]))
    if args.apply:
        print(f"Local snapshot removed: {result['local_snapshot_removed']}")
    else:
        print(f"Review the plan, then rerun with --apply --confirm-device-id {result['device_id']}")
    return 0

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="farmctl", description="Device fleet scan, archive, backup, and restore.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("configure", help="Set up backend/remote/project once, interactively.").set_defaults(func=cmd_configure)

    p_scan = sub.add_parser("scan", help="Fingerprint a device and archive it (prompts for the instance name).")
    p_scan.add_argument("target")
    p_scan.add_argument("--type", choices=["ssh", "adb"], required=True)
    p_scan.set_defaults(func=cmd_scan)

    p_backup = sub.add_parser("backup", help="Push the local device archive to the configured remote.")
    p_backup.add_argument("--project", default=None)
    p_backup.add_argument("--mirror", action="store_true", help="Also delete remote files not present locally.")
    p_backup.add_argument("--dry-run", action="store_true")
    p_backup.add_argument("--compress", dest="compress", action="store_true", default=None,
                           help="Compress into one archive before syncing (overrides configured default).")
    p_backup.add_argument("--no-compress", dest="compress", action="store_false",
                           help="Skip compression even if configured as the default.")
    p_backup.add_argument("--algorithm", default=None, help="Compression algorithm (overrides configured default; blank = auto-pick strongest installed).")
    p_backup.add_argument("--level", type=int, default=None, help="Compression level (overrides configured default).")
    p_backup.add_argument("--no-autopath-registry", action="store_true",
                           help="Skip the secondary backup of autopath's registered-path config.")
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", help="Pull the device archive down from the configured remote (auto-detects compression from the remote's own manifest).")
    p_restore.add_argument("--project", default=None)
    p_restore.add_argument("--dry-run", action="store_true")
    p_restore.add_argument("--fail-loud", action="store_true", default=True,
                            help="Raise immediately on restore failure (default: on).")
    p_restore.add_argument("--no-autopath-registry", action="store_true",
                            help="Skip the secondary restore of autopath's registered-path config.")
    p_restore.set_defaults(func=cmd_restore)

    sub.add_parser("rebuild", help="Reconnect known devices and regenerate SSH/K8s artifacts from the local archive.").set_defaults(func=cmd_rebuild)

    p_merge = sub.add_parser("merge", help="Converge multiple restored source directories into the canonical local archive.")
    p_merge.add_argument("source_dirs", nargs="+")
    p_merge.add_argument("--dry-run", action="store_true")
    p_merge.set_defaults(func=cmd_merge)

    sub.add_parser("status", help="Show current config and local archive summary.").set_defaults(func=cmd_status)
    sub.add_parser("compressors", help="List compression algorithms and which are actually installed on this machine.").set_defaults(func=cmd_compressors)
    sub.add_parser("integrations", help="Interactively detect and select available integration tools.").set_defaults(func=cmd_integrations)

    p_integrations = sub.add_parser("integration", help="Track platform switches, configuration snapshots, and events.")
    integration_sub = p_integrations.add_subparsers(dest="integration_command", required=True)
    p_set = integration_sub.add_parser("set", help="Set desired integration state and optional switches/config JSON.")
    p_set.add_argument("component", choices=COMPONENTS)
    state = p_set.add_mutually_exclusive_group()
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")
    p_set.add_argument("--switches-json", help='JSON object, e.g. \'{"accounting":true,"gpus":true}\'')
    p_set.add_argument("--config-json", help="JSON object containing non-secret config metadata.")
    p_set.set_defaults(func=cmd_integration_set)
    p_show = integration_sub.add_parser("show", help="Show all integration settings or one component.")
    p_show.add_argument("component", nargs="?", choices=COMPONENTS)
    p_show.set_defaults(func=cmd_integration_show)
    p_log = integration_sub.add_parser("log", help="Append an event record for a component.")
    p_log.add_argument("component", choices=COMPONENTS)
    p_log.add_argument("event", help="Event name, such as job.submitted or node.drained.")
    p_log.add_argument("--data-json", help="JSON object with event details.")
    p_log.set_defaults(func=cmd_integration_log)
    p_events = integration_sub.add_parser("events", help="Show recent component events.")
    p_events.add_argument("component", choices=COMPONENTS)
    p_events.add_argument("--limit", type=int, default=50)
    p_events.set_defaults(func=cmd_integration_events)

    p_integrate = sub.add_parser("integrate", help="Fingerprint a node and orchestrate configured tool adapters in order.")
    p_integrate.add_argument("target", help="SSH host or ADB serial")
    p_integrate.add_argument("--type", choices=["ssh", "adb"], required=True)
    p_integrate.add_argument("--apply", action="store_true", help="Persist the snapshot and execute configured adapters (default is plan-only).")
    p_integrate.add_argument("--timeout", type=float, default=120.0, help="Per-adapter timeout in seconds (default: 120).")
    p_integrate.add_argument("--enable-component", choices=COMPONENTS, action="append", default=[],
                             help="Enable this component for this run only; repeatable.")
    p_integrate.add_argument("--skip-component", choices=COMPONENTS, action="append", default=[],
                             help="Skip this component for this run only; repeatable.")
    p_integrate.set_defaults(func=cmd_integrate)

    p_validate = sub.add_parser("validate-adapters", help="Run configured read-only connectivity/credential probes for enabled adapters.")
    p_validate.add_argument("target", help="SSH host or ADB serial to fingerprint")
    p_validate.add_argument("--type", choices=["ssh", "adb"], required=True)
    p_validate.add_argument("--timeout", type=float, default=30.0)
    p_validate.add_argument("--component", choices=COMPONENTS, action="append",
                            help="Validate only this configured integration; repeat for multiple components.")
    p_validate.set_defaults(func=cmd_validate_adapters)

    p_purge = sub.add_parser("purge", help="Plan or execute configured teardowns for a node, then remove its local snapshot.")
    p_purge.add_argument("target", help="SSH host or ADB serial to fingerprint")
    p_purge.add_argument("--type", choices=["ssh", "adb"], required=True)
    p_purge.add_argument("--apply", action="store_true", help="Run explicit teardown adapters and remove this node's local snapshot (default is plan-only).")
    p_purge.add_argument("--confirm-device-id", help="Required with --apply; exact ID printed by the reviewed plan.")
    p_purge.add_argument("--timeout", type=float, default=120.0, help="Per-teardown timeout in seconds (default: 120).")
    p_purge.set_defaults(func=cmd_purge)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
