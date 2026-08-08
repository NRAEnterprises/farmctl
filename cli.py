"""
farmctl — one CLI tying together device fingerprinting/archiving
(device_inventory) and cloud backup/restore (cloud_restore), so the
files device_inventory writes are exactly what backup/restore already
knows how to sync as a whole tree — no per-file registration, no manual
step between "device integrated" and "device backed up."

Typical flow:
    farmctl configure                     # once
    farmctl scan node-pixel7.local --type ssh
    farmctl scan emulator-5554 --type adb
    farmctl backup                        # syncs the whole local archive
    ...later, after a rebuild...
    farmctl restore
    farmctl rebuild
"""

import sys
import argparse

from .config import load_config, save_config, build_backend_from_config
from . import device_inventory as di
from cloud_restore import (
    print_sync_result, restore_or_raise, backup_project, restore_project,
    backup_project_compressed, restore_project_compressed, restore_project_auto,
)
from cloud_restore.compression import detect_available_compressors, recommend_best_available


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
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", help="Pull the device archive down from the configured remote (auto-detects compression from the remote's own manifest).")
    p_restore.add_argument("--project", default=None)
    p_restore.add_argument("--dry-run", action="store_true")
    p_restore.add_argument("--fail-loud", action="store_true", default=True,
                            help="Raise immediately on restore failure (default: on).")
    p_restore.set_defaults(func=cmd_restore)

    sub.add_parser("rebuild", help="Reconnect known devices and regenerate SSH/K8s artifacts from the local archive.").set_defaults(func=cmd_rebuild)

    p_merge = sub.add_parser("merge", help="Converge multiple restored source directories into the canonical local archive.")
    p_merge.add_argument("source_dirs", nargs="+")
    p_merge.add_argument("--dry-run", action="store_true")
    p_merge.set_defaults(func=cmd_merge)

    sub.add_parser("status", help="Show current config and local archive summary.").set_defaults(func=cmd_status)
    sub.add_parser("compressors", help="List compression algorithms and which are actually installed on this machine.").set_defaults(func=cmd_compressors)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
