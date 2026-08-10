"""
Device inventory / fingerprinting / backup module for the compute farm.

Covers:
  - Remote fingerprinting over SSH (Linux nodes) and ADB (Android nodes)
  - Root manager detection (Magisk / KernelSU / APatch) local and remote
  - Wired/USB initial-integration onboarding (ADB / fastboot / serial console)
  - Per-device snapshot persistence, auto-routed to the correct local path
    depending on whether this script itself is running under Termux or Linux
  - Export helpers for Kubernetes ConfigMaps and NATS JetStream KV

Cloud sync of the log directories is handled externally — this module only
writes to the correct local path and leaves distribution to you.
"""

import os
import re
import sys
import glob
import time
import json
import shutil
import hashlib
import platform
import subprocess
from pathlib import Path
from dataclasses import dataclass, field, asdict, is_dataclass
from datetime import datetime, timezone

from autopath import ensure_path, ensure_path_chain, sanitize_component, resolve_unique_path


# =======================================================================
# Local environment detection — determines where THIS script is running
# (the collector), separate from remote-target OS detection below.
# =======================================================================

def detect_local_environment() -> str:
    """Returns 'android' if this script is running inside Termux, 'linux'
    otherwise, 'unknown' if neither is confidently detected."""
    if os.environ.get("PREFIX", "").endswith("com.termux/files/usr"):
        return "android"
    if Path("/data/data/com.termux").exists():
        return "android"
    if platform.system() == "Linux":
        return "linux"
    return "unknown"


def default_log_root() -> Path:
    env = detect_local_environment()
    if env == "android":
        # Real Termux home path: /data/data/com.termux/files/home/
        return Path("/data/data/com.termux/files/home/persist_logs/")
    elif env == "linux":
        return Path("/home/Documents/persist_logs/")
    else:
        # Fallback so the store still lands somewhere sane rather than cwd
        return Path.home() / "persist_logs"


def ensure_log_path(path: Path) -> Path:
    """
    Verifies/creates a log directory, with a safe writability fallback
    if it can't be used as given. This module's own thin wrapper around
    the shared autopath.ensure_path() logic — kept as a named function
    here so existing call sites in this file don't need to change, but
    the actual auto-correction behavior lives in autopath.py (see that
    module for the fallback chain, this project uses ~/persist_logs then
    /tmp as its fallback_root/last_resort, matching prior behavior).
    """
    return ensure_path(path, fallback_root=Path.home() / "persist_logs",
                        last_resort=Path("/tmp/persist_logs"))


# =======================================================================
# Remote transport — wraps SSH or ADB so probes don't care which is used
# =======================================================================

class RemoteExecutor:
    def __init__(self, target: str, connection_type: str, timeout: float = 10.0):
        self.target = target
        self.connection_type = connection_type
        self.timeout = timeout

    def run(self, remote_cmd: str) -> str | None:
        if self.connection_type == "ssh":
            cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                   self.target, remote_cmd]
        elif self.connection_type == "adb":
            cmd = ["adb", "-s", self.target, "shell", remote_cmd]
        else:
            raise ValueError(f"Unknown connection_type: {self.connection_type}")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None


# =======================================================================
# Device fingerprint model
# =======================================================================

@dataclass
class DeviceProfile:
    hostname: str
    connection_type: str
    os_family: str = ""
    distro: str = ""
    distro_version: str = ""
    kernel_release: str = ""
    kernel_arch: str = ""
    android_api_level: str = ""
    android_version: str = ""
    root_info: dict = field(default_factory=dict)
    raw_probes: dict = field(default_factory=dict)


def detect_os_family(exe: RemoteExecutor) -> str:
    if exe.run("getprop ro.build.version.release") is not None:
        return "android"
    if exe.run("uname -s") is not None:
        return "linux"
    return "unknown"


def probe_linux(exe: RemoteExecutor, profile: DeviceProfile) -> None:
    profile.kernel_release = exe.run("uname -r") or ""
    profile.kernel_arch = exe.run("uname -m") or ""

    os_release = exe.run("cat /etc/os-release 2>/dev/null")
    if os_release:
        fields = dict(
            line.split("=", 1) for line in os_release.splitlines() if "=" in line
        )
        profile.distro = fields.get("ID", "").strip('"')
        profile.distro_version = fields.get("VERSION_ID", "").strip('"')
    else:
        for probe_cmd, parser in [
            ("cat /etc/alpine-release", lambda o: ("alpine", o.strip())),
            ("cat /etc/debian_version", lambda o: ("debian", o.strip())),
            ("cat /etc/redhat-release", lambda o: ("rhel", o.strip())),
        ]:
            out = exe.run(probe_cmd)
            if out:
                profile.distro, profile.distro_version = parser(out)
                break

    profile.raw_probes["uname_a"] = exe.run("uname -a") or ""

    # DMI vendor/product — works on most x86 hosts and some SBCs with a
    # real BIOS/DMI table. Not present on all embedded boards (e.g. many
    # Raspberry Pi setups have no DMI), so device-tree model is tried as
    # a fallback. Both are best-effort: absence just means the taxonomy
    # step later falls back to a generic distro-based category instead
    # of a vendor/product one.
    profile.raw_probes["dmi_vendor"] = exe.run(
        "cat /sys/class/dmi/id/sys_vendor 2>/dev/null"
    ) or ""
    profile.raw_probes["dmi_product"] = exe.run(
        "cat /sys/class/dmi/id/product_name 2>/dev/null"
    ) or ""
    if not profile.raw_probes["dmi_product"]:
        profile.raw_probes["dmi_product"] = exe.run(
            "cat /proc/device-tree/model 2>/dev/null"
        ) or ""


# =======================================================================
# Root manager detection — Magisk / KernelSU / APatch
# =======================================================================
#
# All checks here use standard Android property keys and standard
# install paths used by each root manager — none of this is specific to
# any phone manufacturer or model. It runs the same probe sequence
# against any Android device (Pixel, Galaxy, OnePlus, an Android SBC,
# etc.) with no per-model branching anywhere.
#
# This is the fuller version of root detection (multiple binary
# locations per manager, KernelSU's /proc/version signature, a real
# multi-path su search instead of relying on PATH alone) — brought in
# from an earlier draft that existed elsewhere in this project but had
# not actually been merged into the live fingerprinting path until now.

MAGISK_BINARY_PATHS = ["/data/adb/magisk/magisk", "/sbin/magisk", "/system/bin/magisk"]
KERNELSU_BINARY_PATHS = ["/data/adb/ksu/bin/ksud", "/data/adb/ksud", "/system/bin/ksud"]
APATCH_BINARY_PATHS = ["/data/adb/ap/bin/apd", "/data/adb/apd", "/system/bin/apd"]

# Generic su search locations, beyond just whatever `which su` finds on
# PATH — some root manager configurations don't put su on PATH at all.
SU_SEARCH_PATHS = [
    "/system/bin/su", "/system/xbin/su", "/sbin/su",
    "/system/sd/xbin/su", "/system/bin/failsafe/su",
    "/data/local/xbin/su", "/data/local/bin/su", "/data/local/su",
    "/su/bin/su", "/magisk/.core/bin/su", "/debug_ramdisk/su",
]

# Module directories used by Magisk, KernelSU, and APatch — all three
# use the same "one directory per module, containing module.prop"
# layout, so a single search across all of these covers all three
# managers without needing to know which one is active first.
MODULE_SEARCH_ROOTS = [
    "/data/adb/modules", "/data/adb/modules_update",
]


def _first_existing_remote(exe: RemoteExecutor, candidates: list[str]) -> str | None:
    """Finds the first candidate path that exists on the remote device,
    in ONE round trip rather than one per candidate — each exe.run()
    call is a full SSH/ADB invocation, so batching this into a single
    shell loop keeps fingerprinting from getting slow as the candidate
    lists grow."""
    script = "for p in " + " ".join(f'"{p}"' for p in candidates) + '; do [ -e "$p" ] && echo "$p" && break; done'
    return exe.run(script) or None


def _search_module_props_remote(exe: RemoteExecutor, keyword: str) -> list[str]:
    """
    Searches every module.prop file under known Magisk/KernelSU/APatch
    module directories for a case-insensitive keyword match anywhere in
    the file (id, name, or description lines), returning the matching
    module directories. Covers all three managers with one search,
    since they share the same "one directory per module, containing
    module.prop" layout.

    This is how Sui and standalone Zygisk implementations (ZygiskNext,
    ReZygisk, NeoZygisk) get detected — Sui specifically does NOT add
    an su binary to PATH or install a manager app by design, so the
    su-based checks elsewhere in this file will never find it; its
    module.prop is the only reliable remote-shell-visible signal that
    it's present at all.

    Module ID/naming conventions vary across forks and repackagings —
    this is a best-effort keyword search, not a hardcoded exact path,
    the same caveat already noted for KernelSU/APatch su fallbacks
    above.
    """
    roots_glob = " ".join(f"{root}/*/" for root in MODULE_SEARCH_ROOTS)
    script = (
        f'for d in {roots_glob}; do '
        f'[ -f "${{d}}module.prop" ] && grep -qi "{keyword}" "${{d}}module.prop" 2>/dev/null && echo "$d"; '
        f'done 2>/dev/null'
    )
    result = exe.run(script)
    return [line.strip() for line in result.splitlines() if line.strip()] if result else []


def probe_root_remote(exe: RemoteExecutor) -> dict:
    # -- Magisk --
    magisk_binary = _first_existing_remote(exe, MAGISK_BINARY_PATHS)
    magisk_data_dir = bool(exe.run("test -d /data/adb/magisk && echo yes"))
    magisk_prop = exe.run("getprop ro.magisk.version") or exe.run("getprop ro.magisk.versionCode")
    magisk_legacy = bool(exe.run("test -e /sbin/.magisk && echo yes"))
    magisk_app = bool(exe.run("test -d /data/data/com.topjohnwu.magisk && echo yes"))
    magisk_detected = any([magisk_binary, magisk_data_dir, magisk_prop, magisk_legacy, magisk_app])

    # -- KernelSU --
    ksu_binary = _first_existing_remote(exe, KERNELSU_BINARY_PATHS)
    ksu_data_dir = bool(exe.run("test -d /data/adb/ksu && echo yes"))
    ksu_prop = exe.run("getprop ro.kernelsu.version")
    # KernelSU patches the kernel directly, so /proc/version sometimes
    # literally names it — a signal nothing else here gives you.
    ksu_kernel_string = bool(exe.run("grep -qi kernelsu /proc/version 2>/dev/null && echo yes"))
    ksu_app = bool(exe.run("test -d /data/data/me.weishu.kernelsu && echo yes"))
    ksu_detected = any([ksu_binary, ksu_data_dir, ksu_prop, ksu_kernel_string, ksu_app])

    # -- APatch --
    apatch_binary = _first_existing_remote(exe, APATCH_BINARY_PATHS)
    apatch_data_dir = bool(exe.run("test -d /data/adb/ap && echo yes"))
    apatch_prop = exe.run("getprop ro.apatch.version")
    apatch_app = bool(exe.run("test -d /data/data/me.bmax.apatch && echo yes"))
    apatch_detected = any([apatch_binary, apatch_data_dir, apatch_prop, apatch_app])

    detected_manager = None
    if magisk_detected:
        detected_manager = "magisk"
    elif ksu_detected:
        detected_manager = "kernelsu"
    elif apatch_detected:
        detected_manager = "apatch"

    # -- Root access test: presence of a manager != currently having root --
    su_path = exe.run("which su") or _first_existing_remote(exe, SU_SEARCH_PATHS)
    granted = False
    invoke_method = None

    if su_path:
        id_out = exe.run(f"{su_path} -c id")
        if id_out and "uid=0" in id_out:
            granted = True
            invoke_method = f"{su_path} -c"

    if not granted and detected_manager == "kernelsu" and ksu_binary:
        # Best-effort secondary path — ksud's CLI surface has changed
        # across versions, so this is a bonus attempt, not depended on;
        # the generic su check above is the reliable path when it works.
        id_out = exe.run(f"{ksu_binary} root id")
        if id_out and "uid=0" in id_out:
            granted = True
            invoke_method = f"{ksu_binary} root"

    if not granted and detected_manager == "apatch":
        # Best-effort inference of APatch's su-equivalent location,
        # matching its Magisk-compatible module layout — not
        # independently verified against every APatch release.
        apatch_su = "/data/adb/ap/bin/su"
        if exe.run(f"test -e {apatch_su} && echo yes"):
            id_out = exe.run(f"{apatch_su} -c id")
            if id_out and "uid=0" in id_out:
                granted = True
                invoke_method = f"{apatch_su} -c"
                su_path = apatch_su

    # -- Zygisk (prerequisite for Sui) --
    # Magisk 24+ bundles Zygisk internally — no separate module shows
    # up for it, so its presence is inferred from Magisk itself being
    # detected. Whether it's actually TOGGLED ON in Magisk's settings
    # isn't reliably checkable from a plain remote shell (that state
    # lives in Magisk's internal db, not a getprop or a file this probe
    # can read) — this reports Zygisk CAPABILITY, not confirmed
    # enablement, for the Magisk case specifically.
    zygisk_modules = _search_module_props_remote(exe, "zygisk")
    zygisk_capable = bool(zygisk_modules) or (magisk_detected and True)
    zygisk_note = (
        "capability inferred from Magisk itself (built-in since 24.0) — actual "
        "on/off toggle state is not remotely checkable"
        if magisk_detected and not zygisk_modules else
        "standalone Zygisk implementation module found"
        if zygisk_modules else
        "no Zygisk implementation detected"
    )

    # -- Sui: does NOT expose an su binary or manager app by design,
    # so it's only detectable via its module.prop, not via any of the
    # su-based checks above.
    sui_modules = _search_module_props_remote(exe, "sui")
    sui_detected = bool(sui_modules)

    # -- Baseline elevation, independent of su entirely --
    # Captures the plain (no su) uid of the connecting session itself.
    # This is what distinguishes "elevated but not root" (e.g. an ADB
    # shell session, uid=2000, or a Shizuku/Sui-style elevated-but-
    # non-root service) from a full root session (uid=0) or an
    # ordinary unprivileged one — without requiring su to observe it.
    baseline_id = exe.run("id") or ""
    baseline_uid_match = re.search(r"uid=(\d+)\(([^)]*)\)", baseline_id)
    baseline_uid = int(baseline_uid_match.group(1)) if baseline_uid_match else None
    baseline_uid_name = baseline_uid_match.group(2) if baseline_uid_match else None

    if baseline_uid == 0:
        elevation_level = "root_session"  # the connection itself is already root, no su needed
    elif baseline_uid == 2000:
        elevation_level = "shell"  # standard adb shell baseline — elevated above a normal app, not root
    elif baseline_uid is not None and baseline_uid < 10000:
        elevation_level = "system_or_privileged_uid"  # below the app-sandbox uid range, worth noting even if not 0/2000
    else:
        elevation_level = "unprivileged_or_unknown"

    # -- Combined summary: every relevant state and combination in one place --
    if granted and sui_detected:
        combined_state = "root+sui"
    elif granted:
        combined_state = "root"
    elif sui_detected:
        # Sui's module is present but su-based root wasn't confirmed —
        # can happen since Sui deliberately hides the su binary this
        # probe would otherwise use to confirm root.
        combined_state = "sui_present_root_unconfirmed"
    elif elevation_level in ("shell", "system_or_privileged_uid"):
        combined_state = f"non_root_elevated:{elevation_level}"
    else:
        combined_state = "none"

    return {
        "manager": detected_manager,
        "granted": granted,
        "su_path": su_path,
        "invoke_method": invoke_method,
        "zygisk_capable": zygisk_capable,
        "zygisk_modules": zygisk_modules,
        "zygisk_note": zygisk_note,
        "sui_detected": sui_detected,
        "sui_modules": sui_modules,
        "baseline_uid": baseline_uid,
        "baseline_uid_name": baseline_uid_name,
        "elevation_level": elevation_level,
        "combined_state": combined_state,
        "raw": {
            "magisk": {
                "binary": magisk_binary, "data_dir": magisk_data_dir,
                "prop": magisk_prop, "legacy_sbin": magisk_legacy, "app_installed": magisk_app,
            },
            "kernelsu": {
                "binary": ksu_binary, "data_dir": ksu_data_dir,
                "prop": ksu_prop, "kernel_string": ksu_kernel_string, "app_installed": ksu_app,
            },
            "apatch": {
                "binary": apatch_binary, "data_dir": apatch_data_dir,
                "prop": apatch_prop, "app_installed": apatch_app,
            },
        },
    }


def probe_android(exe: RemoteExecutor, profile: DeviceProfile) -> None:
    profile.android_version = exe.run("getprop ro.build.version.release") or ""
    profile.android_api_level = exe.run("getprop ro.build.version.sdk") or ""
    profile.kernel_release = exe.run("uname -r") or ""
    profile.kernel_arch = exe.run("uname -m") or ""

    profile.raw_probes["fingerprint"] = exe.run("getprop ro.build.fingerprint") or ""
    profile.raw_probes["manufacturer"] = exe.run("getprop ro.product.manufacturer") or ""
    profile.raw_probes["model"] = exe.run("getprop ro.product.model") or ""
    profile.raw_probes["brand"] = exe.run("getprop ro.product.brand") or ""

    # Marketing name (e.g. "Widget Pro 9000") is NOT a universal Android
    # property — only some OEM builds expose one, under inconsistent
    # keys. This tries the known candidates and falls back to empty if
    # none exist; the taxonomy step degrades gracefully to using the raw
    # model number instead when no marketing name is available.
    for candidate_prop in (
        "ro.product.marketing_name",
        "ro.config.marketing_name",
        "ro.product.model.marketing_name",
    ):
        marketing = exe.run(f"getprop {candidate_prop}")
        if marketing:
            profile.raw_probes["marketing_name"] = marketing
            break
    else:
        profile.raw_probes["marketing_name"] = ""

    profile.root_info = probe_root_remote(exe)


def fingerprint_device(target: str, connection_type: str) -> DeviceProfile:
    exe = RemoteExecutor(target, connection_type)
    profile = DeviceProfile(hostname=target, connection_type=connection_type)
    profile.os_family = detect_os_family(exe)

    if profile.os_family == "android":
        probe_android(exe, profile)
    elif profile.os_family == "linux":
        probe_linux(exe, profile)

    return profile


# =======================================================================
# Wired / USB initial-integration onboarding
# =======================================================================

@dataclass
class UsbDeviceProfile:
    usb_path: str
    vendor_id: str = ""
    product_id: str = ""
    serial: str = ""
    detected_role: str = ""
    device_node: str = ""
    fingerprint: DeviceProfile | None = None


KNOWN_ANDROID_VIDS = {"18d1", "04e8", "22b8", "0bb4", "12d1", "2717", "2a70"}


def list_usb_devices() -> list[dict]:
    try:
        result = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    devices = []
    pattern = re.compile(r"Bus (\d+) Device (\d+): ID ([0-9a-f]{4}):([0-9a-f]{4})(.*)")
    for line in result.stdout.splitlines():
        m = pattern.match(line)
        if m:
            bus, dev, vid, pid, desc = m.groups()
            devices.append({
                "bus": bus, "device": dev,
                "vendor_id": vid, "product_id": pid,
                "description": desc.strip(),
            })
    return devices


def list_serial_ports() -> list[str]:
    candidates = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
    return sorted(candidates)


def confirm_adb(timeout: float = 5.0) -> list[str]:
    try:
        subprocess.run(["adb", "start-server"], capture_output=True, timeout=timeout)
        result = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    serials = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if line and "\tdevice" in line:
            serials.append(line.split("\t")[0])
        elif line and "\tunauthorized" in line:
            serials.append(f"UNAUTHORIZED:{line.split(chr(9))[0]}")
    return serials


def confirm_fastboot(timeout: float = 5.0) -> list[str]:
    try:
        result = subprocess.run(
            ["fastboot", "devices"], capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return [line.split("\t")[0] for line in result.stdout.splitlines() if line.strip()]


def probe_serial_console(port: str, baud: int = 115200, timeout: float = 2.0) -> bool:
    try:
        import serial  # pyserial
    except ImportError:
        return False

    try:
        with serial.Serial(port, baud, timeout=timeout) as ser:
            ser.write(b"\n")
            time.sleep(0.3)
            response = ser.read(256)
            return len(response) > 0
    except (serial.SerialException, OSError):
        return False


def onboard_wired_devices() -> list[UsbDeviceProfile]:
    profiles = []

    for serial_id in confirm_adb():
        profile = UsbDeviceProfile(
            usb_path="", serial=serial_id, detected_role="adb", device_node=serial_id,
        )
        if not serial_id.startswith("UNAUTHORIZED"):
            profile.fingerprint = fingerprint_device(serial_id, "adb")
        profiles.append(profile)

    for serial_id in confirm_fastboot():
        profiles.append(UsbDeviceProfile(
            usb_path="", serial=serial_id, detected_role="fastboot", device_node=serial_id,
        ))

    for port in list_serial_ports():
        if probe_serial_console(port):
            profiles.append(UsbDeviceProfile(
                usb_path="", detected_role="serial_console", device_node=port,
            ))

    return profiles


# =======================================================================
# Taxonomy-based archive — end-of-runtime hook for the repair/integration
# tool
# =======================================================================
#
# Separate from the flat hash-keyed DeviceLogStore below (which is for
# automated fleet backup/rebuild). This is a human-facing archive: call
# it once your repair/integration tool finishes working on a device. It
# automatically builds a deep category path — manufacturer -> product
# line -> model for Android, vendor -> product -> distro for Linux — and
# only prompts you for one thing: the name of this specific instance's
# file. Ten identical S25 Ultras all converge under the same
# Samsung/Galaxy/S25-Ultra/ folder; each unit gets its own file inside
# it, named by you at the end of that unit's run.
#
# The category layers are best-effort. Android marketing names (e.g.
# "Widget Pro 9000") aren't a universal property — only some OEM builds
# expose one; when it's not available, the folder falls back to the raw
# model number instead of a marketing name. Similarly, Linux DMI vendor/
# product data isn't present on every board (Raspberry Pi and similar
# have no DMI table), so that falls back to a device-tree model string,
# and finally to a generic distro-based category if neither is present.
# None of this affects correctness of the stored data — it only affects
# how deep/specific the automatic folder path ends up being.
#
# Sanitization uses the shared autopath.sanitize_component() rather than
# a local copy — one implementation, reused everywhere in this file and
# in any other project that imports autopath.

def _split_marketing_name(marketing_name: str, raw_model: str) -> tuple[str, str]:
    """
    Best-effort split of a marketing name into (product_line, specific_model).
    "Widget Pro 9000" -> ("Widget", "Pro 9000")
    If no marketing name is available, falls back to a generic "Device"
    product-line bucket with the raw model number as the specific model —
    still fully automatic, just less descriptive than a marketing name
    would give.
    """
    marketing_name = (marketing_name or "").strip()
    if marketing_name:
        parts = marketing_name.split(None, 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return "Device", parts[0]
    return "Device", (raw_model or "unknown-model")


def determine_taxonomy_path(profile: dict) -> list[str]:
    """
    Returns the ordered, auto-derived category path for a device
    (most general -> most specific), BEFORE the user-chosen instance
    file name. This is the fully-automatic part — nothing here ever
    prompts for input.
    """
    os_family = profile.get("os_family", "unknown")
    raw_probes = profile.get("raw_probes", {}) or {}

    if os_family == "android":
        manufacturer = raw_probes.get("manufacturer") or raw_probes.get("brand") or "unknown-manufacturer"
        marketing_name = raw_probes.get("marketing_name", "")
        raw_model = raw_probes.get("model", "")
        product_line, specific_model = _split_marketing_name(marketing_name, raw_model)
        return [
            sanitize_component(manufacturer),
            sanitize_component(product_line),
            sanitize_component(specific_model),
        ]

    if os_family == "linux":
        vendor = raw_probes.get("dmi_vendor", "")
        product = raw_probes.get("dmi_product", "")
        distro = profile.get("distro") or "generic-linux"
        if vendor and product:
            return [
                sanitize_component(vendor),
                sanitize_component(product),
                sanitize_component(distro),
            ]
        # No DMI/device-tree identity available — fall back to a
        # distro-only category rather than a vendor/product guess.
        return ["Linux", sanitize_component(distro)]

    return ["unknown-os-family"]


def prompt_for_instance_name(default_hint: str = "") -> str:
    """
    Prompts the operator for the name of THIS specific device's file —
    the one piece of the whole path that stays manual by design.
    If there's no interactive terminal available (e.g. this hook runs
    under a headless/CI invocation of the repair tool), it falls back
    to a timestamp-based name instead of blocking forever on input().
    """
    if not sys.stdin.isatty():
        fallback = f"unit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        print(f"(no interactive terminal available — using default name: {fallback})")
        return fallback

    prompt_text = f"Name this device's log file{f' [{default_hint}]' if default_hint else ''}: "
    entered = input(prompt_text).strip()
    if not entered:
        entered = default_hint or f"unit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    return sanitize_component(entered)


def write_taxonomy_log(profile_obj, archive_root: Path | None = None) -> Path:
    """
    THE end-of-runtime hook. Call this after your repair/integration
    tool finishes working on a device, passing the DeviceProfile (or
    UsbDeviceProfile) it just fingerprinted.

    1. Auto-derives the full category path (manufacturer/product-line/
       model, or vendor/product/distro) — fully automatic.
    2. Creates every directory in that chain if it doesn't exist yet,
       whether this is the first unit of this model ever logged or the
       fiftieth — same auto-create/auto-correct/writability check used
       everywhere else in this module (ensure_log_path).
    3. Prompts ONLY for the specific instance's file name.
    4. If that exact file name already exists in this category folder
       (e.g. you type the same name twice for two different physical
       units by mistake), auto-corrects by appending a numeric suffix
       rather than silently overwriting a previous unit's data.
    5. Writes the full fingerprint to that file and returns its path.
    """
    root = archive_root or default_log_root()
    profile_dict = _to_serializable(profile_obj)

    # UsbDeviceProfile wraps the real fingerprint one level down —
    # unwrap it so taxonomy derivation sees the actual device data.
    if profile_dict.get("fingerprint"):
        taxonomy_source = profile_dict["fingerprint"]
    else:
        taxonomy_source = profile_dict

    category_parts = determine_taxonomy_path(taxonomy_source)
    category_dir = ensure_path_chain(root, *category_parts)

    default_hint = (
        taxonomy_source.get("hostname")
        or (taxonomy_source.get("raw_probes") or {}).get("model")
        or ""
    )
    instance_name = prompt_for_instance_name(default_hint)

    record = {
        "device_id": _device_id(profile_obj),
        "snapshot_time": datetime.now(timezone.utc).isoformat(),
        "category_path": category_parts,
        "instance_name": instance_name,
        "profile": profile_dict,
    }

    file_path = resolve_unique_path(category_dir, instance_name, extension=".json")
    if file_path.name != f"{sanitize_component(instance_name)}.json":
        print(f"note: a file for '{instance_name}' already exists in this category — "
              f"writing this unit as '{file_path.name}' instead so nothing is overwritten")

    file_path.write_text(json.dumps(record, indent=2, sort_keys=True))
    print(f"Device archived to: {file_path}")
    return file_path


def integration_complete_hook(target: str, connection_type: str,
                               archive_root: Path | None = None) -> Path:
    """
    Convenience wrapper for the repair tool: pass the same
    (target, connection_type) you just finished integrating and this
    fingerprints it fresh, then runs the full taxonomy-archive-and-prompt
    flow above. Call this as the very last step of your integration
    process for each device.
    """
    profile = fingerprint_device(target, connection_type)
    return write_taxonomy_log(profile, archive_root=archive_root)


# =======================================================================
# Persistence — snapshots routed to the correct local path automatically
# =======================================================================

def _to_serializable(obj):
    if is_dataclass(obj):
        return {k: _to_serializable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_serializable(v) for v in obj]
    return obj


def _canonical_identity_seed(hostname: str, serial: str, usb_path: str, build_fingerprint: str) -> str:
    """
    Normalizes identity fields before hashing so the SAME physical device
    always converges to the SAME device_id, regardless of which collector
    or source produced the snapshot (case differences, incidental
    whitespace, etc. must not create phantom duplicate devices).
    """
    parts = [
        (hostname or "").strip().lower(),
        (serial or "").strip().lower(),
        (usb_path or "").strip().lower(),
        (build_fingerprint or "").strip().lower(),
    ]
    return "|".join(parts)


def _extract_build_fingerprint(profile) -> str:
    """Android build fingerprint, if present. Handles both a plain
    DeviceProfile (raw_probes directly on it) and a UsbDeviceProfile
    (raw_probes nested one level down under .fingerprint)."""
    raw_probes = getattr(profile, "raw_probes", None)
    if isinstance(raw_probes, dict) and raw_probes.get("fingerprint"):
        return raw_probes["fingerprint"]

    nested = getattr(profile, "fingerprint", None)
    if nested is not None:
        nested_raw = getattr(nested, "raw_probes", None)
        if isinstance(nested_raw, dict):
            return nested_raw.get("fingerprint", "")
    return ""


def _device_id(profile) -> str:
    """Computes a stable device_id from a live profile object (used at
    write time, right after fingerprinting)."""
    seed = _canonical_identity_seed(
        hostname=getattr(profile, "hostname", ""),
        serial=getattr(profile, "serial", ""),
        usb_path=getattr(profile, "usb_path", ""),
        build_fingerprint=_extract_build_fingerprint(profile),
    )
    if seed.replace("|", "") == "":
        # No stable identity available at all — fall back to a
        # time-seeded id rather than colliding every unidentifiable
        # device onto one record.
        seed = str(time.time())
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def _device_id_from_profile_dict(profile: dict) -> str:
    """Same computation as _device_id, but operating on an already-loaded
    JSON dict (used when re-deriving identity during a cloud-sync merge,
    so a record's canonical id doesn't depend on trusting whatever id the
    source happened to write it under)."""
    build_fingerprint = (profile or {}).get("raw_probes", {}).get("fingerprint", "")
    if not build_fingerprint:
        nested = (profile or {}).get("fingerprint") or {}
        build_fingerprint = (nested.get("raw_probes") or {}).get("fingerprint", "")

    seed = _canonical_identity_seed(
        hostname=(profile or {}).get("hostname", ""),
        serial=(profile or {}).get("serial", ""),
        usb_path=(profile or {}).get("usb_path", ""),
        build_fingerprint=build_fingerprint,
    )
    if seed.replace("|", "") == "":
        return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:16]
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


class DeviceLogStore:
    """
    Writes device snapshots to the correct local path automatically:
      - Termux (Android):  /data/data/com.termux/files/home/persist_logs/
      - Linux:              /home/Documents/persist_logs/
      - Anything else:      ~/persist_logs/  (fallback, so it never
                             silently writes to the current working dir)

    Pass `root=` explicitly to override auto-detection.
    """

    def __init__(self, root: str | Path | None = None):
        target = Path(root).expanduser() if root else default_log_root()
        self.root = ensure_log_path(target)

    def _device_dir(self, device_id: str) -> Path:
        # self.root is already verified writable, but routing this
        # through ensure_path too (rather than a bare mkdir) keeps
        # every directory creation in the project going through the
        # one shared autopath implementation, with no exceptions.
        return ensure_path(self.root / device_id,
                            fallback_root=self.root, last_resort=self.root)

    def write_snapshot(self, profile) -> Path:
        device_id = _device_id(profile)
        device_dir = self._device_dir(device_id)

        record = {
            "device_id": device_id,
            "snapshot_time": datetime.now(timezone.utc).isoformat(),
            "profile": _to_serializable(profile),
        }

        timestamp = record["snapshot_time"].replace(":", "-")
        snapshot_path = device_dir / f"{timestamp}.json"
        snapshot_path.write_text(json.dumps(record, indent=2, sort_keys=True))

        latest_path = device_dir / "latest.json"
        latest_path.write_text(json.dumps(record, indent=2, sort_keys=True))

        return snapshot_path

    def load_latest(self, device_id: str) -> dict | None:
        latest_path = self.root / device_id / "latest.json"
        if not latest_path.exists():
            return None
        return json.loads(latest_path.read_text())

    def list_devices(self) -> list[str]:
        return [p.name for p in self.root.iterdir() if p.is_dir() and p.name != "_failed_probes"]

    def full_inventory(self) -> dict:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "device_count": len(self.list_devices()),
            "devices": {
                device_id: self.load_latest(device_id)
                for device_id in self.list_devices()
            },
        }


# =======================================================================
# Export helpers — Kubernetes ConfigMap / NATS JetStream KV
# =======================================================================

def export_k8s_configmap(store: DeviceLogStore, namespace: str = "compute-farm",
                          name: str = "device-inventory") -> dict:
    data = {}
    for device_id in store.list_devices():
        latest = store.load_latest(device_id)
        if latest:
            data[f"{device_id}.json"] = json.dumps(latest)

    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": "device-log-store"},
        },
        "data": data,
    }


def write_k8s_manifest(store: DeviceLogStore, output_path: str = "device-inventory-configmap.json") -> str:
    manifest = export_k8s_configmap(store)
    Path(output_path).write_text(json.dumps(manifest, indent=2))
    return output_path


async def export_nats_jetstream(store: DeviceLogStore, nats_url: str = "nats://localhost:4222",
                                 bucket: str = "device-inventory"):
    """Requires: pip install nats-py"""
    import nats

    nc = await nats.connect(nats_url)
    js = nc.jetstream()

    try:
        kv = await js.key_value(bucket)
    except Exception:
        kv = await js.create_key_value(bucket=bucket)

    for device_id in store.list_devices():
        latest = store.load_latest(device_id)
        if latest:
            await kv.put(device_id, json.dumps(latest).encode())

    await nc.close()


# =======================================================================
# Cloud-sync restore / multi-source convergence
# =======================================================================
#
# Your cloud sync tool handles moving files up/down — this section handles
# what happens on the DOWNLOAD side: taking one or more restored source
# directories (e.g. separate sync folders per device, or per collector,
# or a staging area your sync client dumps everything into) and merging
# them into the single canonical local path this script already writes
# to (the same path detect_local_environment()/default_log_root() would
# pick on this machine — so restore always lands back where backup
# would have written it, with no separate configuration needed).
#
# Convergence rules:
#   - Device identity is RE-DERIVED from each record's own profile data
#     (via _device_id_from_profile_dict), not trusted from whatever id
#     the source directory/filename says. This means the same physical
#     device uploaded from two different collectors — even if one wrote
#     it under a stale or mismatched id — converges onto one id here.
#   - Snapshots are de-duplicated by CONTENT hash, not by filename or
#     timestamp. Two sources uploading the literal same capture (e.g.
#     because a node itself backs up locally AND a central collector
#     also grabbed it) collapse to a single stored file instead of two
#     redundant copies.
#   - Snapshots that differ in content (a genuinely later capture of the
#     same device from a different source) are kept as separate
#     timestamped entries — convergence removes exact duplicates, it
#     does not discard real history.
#   - "latest.json" is recomputed after merging as whichever unique
#     snapshot has the newest snapshot_time, regardless of which source
#     directory it came from.

def _load_all_snapshots_from_source(source_root: Path) -> list[dict]:
    """Walks one restored source directory and loads every timestamped
    snapshot record found under it (skips latest.json — it's always a
    duplicate of the newest timestamped file, so loading it too would
    just be redundant work for the dedup step below)."""
    records = []
    source_root = Path(source_root)
    if not source_root.exists():
        return records

    for device_dir in source_root.iterdir():
        if not device_dir.is_dir() or device_dir.name == "_failed_probes":
            continue
        for snapshot_file in device_dir.glob("*.json"):
            if snapshot_file.name == "latest.json":
                continue
            try:
                records.append(json.loads(snapshot_file.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
    return records


def _content_hash(profile: dict) -> str:
    """Hashes the profile content only (not snapshot_time/device_id), so
    the exact same capture uploaded from two sources is recognized as a
    true duplicate rather than two distinct historical snapshots."""
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


def merge_snapshot_sources(source_dirs: list[str | Path],
                            destination_store: DeviceLogStore | None = None,
                            dry_run: bool = False) -> dict:
    """
    Reads every snapshot from all given source_dirs, re-derives canonical
    device identity for each, de-duplicates by content, and writes the
    converged result into destination_store (auto-routed to this
    machine's default path if not given — same path backup_all_devices
    would use).

    dry_run=True computes and reports what WOULD be written without
    touching disk — useful to sanity-check a merge before committing it.
    """
    destination_store = destination_store or DeviceLogStore()

    all_records = []
    per_source_counts = {}
    for src in source_dirs:
        loaded = _load_all_snapshots_from_source(Path(src).expanduser())
        per_source_counts[str(src)] = len(loaded)
        all_records.extend(loaded)

    grouped: dict[str, list[dict]] = {}
    for record in all_records:
        profile = record.get("profile", {})
        canonical_id = _device_id_from_profile_dict(profile)
        grouped.setdefault(canonical_id, []).append(record)

    stats = {
        "sources_scanned": len(source_dirs),
        "records_per_source": per_source_counts,
        "records_found_total": len(all_records),
        "unique_devices_after_convergence": len(grouped),
        "snapshots_written": 0,
        "duplicates_skipped": 0,
        "dry_run": dry_run,
        "destination": str(destination_store.root),
    }

    for canonical_id, records in grouped.items():
        seen_hashes = set()
        unique_records = []
        for record in sorted(records, key=lambda r: r.get("snapshot_time", "")):
            profile = record.get("profile", {})
            h = _content_hash(profile)
            if h in seen_hashes:
                stats["duplicates_skipped"] += 1
                continue
            seen_hashes.add(h)
            unique_records.append(record)

        if dry_run:
            stats["snapshots_written"] += len(unique_records)
            continue

        device_dir = destination_store._device_dir(canonical_id)
        for record in unique_records:
            # Correct the stored id to the re-derived canonical one, in
            # case the source had it filed under a stale/mismatched id.
            record["device_id"] = canonical_id
            timestamp = record.get(
                "snapshot_time", datetime.now(timezone.utc).isoformat()
            ).replace(":", "-")
            snapshot_path = device_dir / f"{timestamp}.json"
            if not snapshot_path.exists():
                snapshot_path.write_text(json.dumps(record, indent=2, sort_keys=True))
                stats["snapshots_written"] += 1

        if unique_records:
            newest = max(unique_records, key=lambda r: r.get("snapshot_time", ""))
            (device_dir / "latest.json").write_text(json.dumps(newest, indent=2, sort_keys=True))

    return stats


def print_merge_report(stats: dict) -> None:
    print(f"\n{'='*60}")
    print(f"{'DRY RUN — ' if stats['dry_run'] else ''}CLOUD-SYNC MERGE REPORT")
    print(f"{'='*60}")
    print(f"Sources scanned:              {stats['sources_scanned']}")
    for src, count in stats["records_per_source"].items():
        print(f"  {src}: {count} snapshot(s)")
    print(f"Total records found:          {stats['records_found_total']}")
    print(f"Unique devices after merge:   {stats['unique_devices_after_convergence']}")
    print(f"Snapshots written:            {stats['snapshots_written']}")
    print(f"Exact duplicates skipped:     {stats['duplicates_skipped']}")
    print(f"Destination path:             {stats['destination']}\n")


def restore_from_cloud_sync(source_dirs: list[str | Path],
                             destination_store: DeviceLogStore | None = None,
                             dry_run: bool = False) -> dict:
    """
    Top-level entry point for the download/restore side. Call this after
    your cloud sync tool has pulled logs down into one or more local
    staging directories — this converges them into the single canonical
    path (auto-detected for this machine, same as backup writes to)
    that rebuild_from_logs() and the K8s/NATS exporters read from.

    Example:
        restore_from_cloud_sync([
            "/mnt/cloud-restore/node-a-uploads",
            "/mnt/cloud-restore/node-b-uploads",
        ])
    """
    destination_store = destination_store or DeviceLogStore()
    stats = merge_snapshot_sources(source_dirs, destination_store, dry_run=dry_run)
    print_merge_report(stats)
    return stats


def backup_all_devices(remote_targets: list[tuple[str, str]],
                        store: DeviceLogStore | None = None) -> DeviceLogStore:
    """
    remote_targets: list of (target, connection_type) tuples, e.g.
        [("nodeA.local", "ssh"), ("emulator-5554", "adb")]
    Combines with onboard_wired_devices() for anything currently on USB.
    Store auto-routes to the correct local path if not explicitly given.
    """
    store = store or DeviceLogStore()

    for target, conn_type in remote_targets:
        try:
            profile = fingerprint_device(target, conn_type)
            store.write_snapshot(profile)
        except Exception as e:
            fallback_dir = store.root / "_failed_probes"
            fallback_dir.mkdir(exist_ok=True)
            (fallback_dir / f"{target}-{int(time.time())}.json").write_text(
                json.dumps({"target": target, "connection_type": conn_type, "error": str(e)})
            )

    for wired_profile in onboard_wired_devices():
        store.write_snapshot(wired_profile)

    return store


# =======================================================================
# Rebuild / restore — turns saved logs back into actionable targets
# =======================================================================
#
# This is the consumption side. It reads whatever DeviceLogStore has on
# disk (or was synced back down from your cloud target) and reconstructs
# the pieces you need to reintegrate a device after a full network wipe:
# an SSH config, an ADB reconnect list, and K8s node labels — plus a
# reachability diff so you know which devices are missing and need to be
# physically re-onboarded rather than just reconnected.

@dataclass
class RebuildStatus:
    device_id: str
    hostname: str
    connection_type: str
    reachable: bool
    last_seen: str          # snapshot_time from the stored record
    detail: str = ""        # error message if unreachable


def _extract_profile_dict(record: dict) -> dict:
    """Snapshots wrap the profile under record['profile']; USB-onboarded
    devices nest it one level deeper under record['profile']['fingerprint']
    when the fingerprint itself came from a DeviceProfile. Handle both."""
    profile = record.get("profile", {})
    if profile.get("fingerprint"):
        return profile["fingerprint"]
    return profile


def check_reachability(record: dict, timeout: float = 5.0) -> RebuildStatus:
    """
    Re-probes a device using the connection info from its last saved
    snapshot. This is the core of "what's actually still alive" after
    a rebuild — a stored log tells you what a device WAS, this tells
    you whether it's reachable again right now.
    """
    profile = _extract_profile_dict(record)
    device_id = record.get("device_id", "unknown")
    hostname = profile.get("hostname", "")
    conn_type = profile.get("connection_type", "")
    last_seen = record.get("snapshot_time", "")

    if not hostname or not conn_type:
        return RebuildStatus(
            device_id=device_id, hostname=hostname, connection_type=conn_type,
            reachable=False, last_seen=last_seen,
            detail="incomplete record — missing hostname/connection_type",
        )

    exe = RemoteExecutor(hostname, conn_type, timeout=timeout)
    result = exe.run("echo alive")
    reachable = result == "alive"

    return RebuildStatus(
        device_id=device_id, hostname=hostname, connection_type=conn_type,
        reachable=reachable, last_seen=last_seen,
        detail="" if reachable else "no response on last known connection info",
    )


def rebuild_reachability_report(store: DeviceLogStore, timeout: float = 5.0) -> list[RebuildStatus]:
    """Checks every device in the store, returns a status list you can
    use to decide what to reconnect automatically vs. what needs a human
    to go re-plug/re-image the physical device."""
    statuses = []
    for device_id in store.list_devices():
        record = store.load_latest(device_id)
        if record:
            statuses.append(check_reachability(record, timeout=timeout))
    return statuses


def print_rebuild_report(statuses: list[RebuildStatus]) -> None:
    reachable = [s for s in statuses if s.reachable]
    missing = [s for s in statuses if not s.reachable]

    print(f"\n{'='*60}")
    print(f"REBUILD REACHABILITY REPORT")
    print(f"{'='*60}")
    print(f"Reachable: {len(reachable)}   Missing: {len(missing)}   Total: {len(statuses)}\n")

    if reachable:
        print("-- Reachable (will auto-reconnect) --")
        for s in reachable:
            print(f"  [{s.connection_type:4}] {s.hostname:30} (last seen {s.last_seen})")

    if missing:
        print("\n-- MISSING — needs physical re-onboarding --")
        for s in missing:
            print(f"  [{s.connection_type:4}] {s.hostname:30} last seen {s.last_seen} — {s.detail}")
    print()


# --- SSH config regeneration -------------------------------------------

def generate_ssh_config(store: DeviceLogStore, output_path: str = "rebuilt_ssh_config") -> str:
    """
    Produces an SSH config block per Linux/ssh-connected device, so you
    can `cat rebuilt_ssh_config >> ~/.ssh/config` and immediately have
    Host aliases for every previously-known node. Uses hostname as both
    the Host alias and HostName since that's all the stored record
    reliably has — adjust User/IdentityFile per your actual key setup.
    """
    lines = []
    for device_id in store.list_devices():
        record = store.load_latest(device_id)
        if not record:
            continue
        profile = _extract_profile_dict(record)
        if profile.get("connection_type") != "ssh":
            continue
        hostname = profile.get("hostname", "")
        if not hostname:
            continue

        lines.append(f"Host {hostname}")
        lines.append(f"    HostName {hostname}")
        lines.append(f"    User root")
        lines.append(f"    StrictHostKeyChecking accept-new")
        lines.append("")

    content = "\n".join(lines)
    Path(output_path).write_text(content)
    return output_path


# --- ADB reconnect target list -------------------------------------------

def generate_adb_target_list(store: DeviceLogStore) -> list[str]:
    """
    Returns hostnames/serials for every ADB-connected device on record.
    For network ADB targets (host:port), feed these straight into
    `adb connect <target>` in a loop. USB-serial targets won't reconnect
    this way — they show up in the missing/reachability report instead,
    since USB presence has to be checked physically, not dialed remotely.
    """
    targets = []
    for device_id in store.list_devices():
        record = store.load_latest(device_id)
        if not record:
            continue
        profile = _extract_profile_dict(record)
        if profile.get("connection_type") == "adb":
            hostname = profile.get("hostname", "")
            if hostname and ":" in hostname:  # network adb target, not a USB serial
                targets.append(hostname)
    return targets


def reconnect_adb_targets(store: DeviceLogStore, timeout: float = 5.0) -> dict[str, bool]:
    """Actually issues `adb connect` for every network ADB target on
    record. Returns {target: success} so you can see what came back up."""
    results = {}
    for target in generate_adb_target_list(store):
        try:
            r = subprocess.run(
                ["adb", "connect", target],
                capture_output=True, text=True, timeout=timeout,
            )
            results[target] = "connected" in r.stdout.lower()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            results[target] = False
    return results


# --- Kubernetes node label regeneration -----------------------------------

def generate_k8s_node_labels(store: DeviceLogStore) -> dict[str, dict[str, str]]:
    """
    Builds a {node_name: {label_key: label_value}} map from stored
    fingerprints, suitable for feeding into
    `kubectl label node <name> key=value` in a loop, so a rebuilt
    cluster gets nodes labeled by OS family/distro/arch/root-manager
    without re-probing everything from scratch.
    """
    node_labels = {}
    for device_id in store.list_devices():
        record = store.load_latest(device_id)
        if not record:
            continue
        profile = _extract_profile_dict(record)
        hostname = profile.get("hostname", "")
        if not hostname:
            continue

        labels = {
            "farm/os-family": profile.get("os_family", "unknown"),
            "farm/kernel-arch": profile.get("kernel_arch", "unknown") or "unknown",
        }
        if profile.get("os_family") == "linux":
            labels["farm/distro"] = profile.get("distro", "unknown") or "unknown"
        elif profile.get("os_family") == "android":
            labels["farm/android-version"] = profile.get("android_version", "unknown") or "unknown"
            root_manager = (profile.get("root_info") or {}).get("manager")
            if root_manager:
                labels["farm/root-manager"] = root_manager

        # Sanitize for k8s label value rules (alphanumeric, -, _, ., max 63 chars)
        labels = {k: re.sub(r"[^A-Za-z0-9_.\-]", "-", v)[:63] for k, v in labels.items()}
        node_labels[hostname] = labels

    return node_labels


def write_k8s_label_script(store: DeviceLogStore, output_path: str = "rebuilt_node_labels.sh") -> str:
    """Writes a shell script of `kubectl label node` commands, one per
    device, ready to run against a freshly rebuilt cluster."""
    node_labels = generate_k8s_node_labels(store)
    lines = ["#!/bin/sh", "set -e", ""]
    for node_name, labels in node_labels.items():
        label_args = " ".join(f"{k}={v}" for k, v in labels.items())
        lines.append(f"kubectl label node {node_name} {label_args} --overwrite")
    content = "\n".join(lines) + "\n"
    Path(output_path).write_text(content)
    os.chmod(output_path, 0o755)
    return output_path


# --- Top-level rebuild entry point -----------------------------------------

def rebuild_from_logs(store: DeviceLogStore | None = None, timeout: float = 5.0) -> dict:
    """
    One call that does the full rebuild-prep sequence:
      1. Loads the persisted store (auto-routed path if not given)
      2. Checks reachability of every known device
      3. Regenerates SSH config, reconnects network ADB targets,
         and writes a K8s labeling script
      4. Prints a report of what's back vs. what's still missing
    Returns a dict summary for programmatic use.
    """
    store = store or DeviceLogStore()

    statuses = rebuild_reachability_report(store, timeout=timeout)
    print_rebuild_report(statuses)

    ssh_config_path = generate_ssh_config(store)
    adb_results = reconnect_adb_targets(store, timeout=timeout)
    k8s_script_path = write_k8s_label_script(store)

    return {
        "store_root": str(store.root),
        "statuses": statuses,
        "ssh_config_written": ssh_config_path,
        "adb_reconnect_results": adb_results,
        "k8s_label_script_written": k8s_script_path,
    }


if __name__ == "__main__":
    # Example usage — replace with your actual fleet targets
    targets = [
        # ("node-name.local", "ssh"),
        # ("emulator-5554", "adb"),
    ]
    store = backup_all_devices(targets)
    print(f"Logs written to: {store.root}")
    print(f"Devices tracked: {store.list_devices()}")

    # Restore-from-cloud-sync example (run this on a freshly rebuilt
    # system, after your cloud sync tool has pulled logs down into one
    # or more local staging directories):
    # restore_from_cloud_sync([
    #     "/mnt/cloud-restore/source-a",
    #     "/mnt/cloud-restore/source-b",
    # ])

    # Rebuild-from-logs example, run after the restore above:
    # rebuild_from_logs()
