# farmctl

Device fleet scanning, taxonomy-based archiving, and cloud backup/restore
for a self-hosted compute farm — SSH-reachable Linux nodes and ADB-reachable
Android nodes alike.

This is a **private, domain-specific consumer project**, not a core logic
package. It requires both [`autopath`](https://github.com/NRAEnterprises/autopath-package)
and [`cloud-restore`](https://github.com/NRAEnterprises/cloud-restore-package) as real,
separately-installed prerequisites — it consumes and configures them, it
does not vendor a copy of either or shape how either one works. Neither
core package knows `farmctl` exists.

## Install

```sh
curl -LsSf https://raw.githubusercontent.com/NRAEnterprises/farmctl-package/main/install.sh | sh
```

This installs `autopath` and `cloud-restore` first, as explicit prerequisite
steps, then `farmctl` itself. All three are pure Python stdlib — nothing
here needs compiling, on Linux or Termux.

## Quick start

```sh
farmctl configure                        # backend, remote, project name — once
farmctl scan node-1.local --type ssh
farmctl scan emulator-5554 --type adb
farmctl backup
```

Later, after a rebuild:

```sh
farmctl restore
farmctl rebuild
```

## What each command does

### `farmctl configure`

Interactive, one-time setup: which backend (`rsync` or `rclone`), the
remote target, the project namespace, and whether to compress backups by
default (and with which algorithm/level). Saved to
`~/.config/farmctl/config.json`. Every other command reads this
automatically — nothing else needs these passed as flags.

### `farmctl scan <target> --type ssh|adb`

Fingerprints a device (OS, distro/Android version, kernel, root manager if
present) and archives it into a deep, automatic category tree — e.g. ten
identical units of the same model all converge under
a single `vendor/model-line/model/` folder, and you're prompted only for that specific
unit's file name within that folder. If the device can't be reached or
identified, nothing is archived and the command exits with an error —
earlier versions of this silently wrote a junk record for unreachable
hosts; that's fixed.

### `farmctl backup` / `farmctl restore`

Push/pull the entire local device archive via the backend configured in
`farmctl configure`. Compression (if enabled) and its algorithm are
handled automatically — `restore` reads the remote's own manifest to know
whether the data is compressed and how, so nothing about the compression
mode needs to be remembered or passed by hand, even if it changes between
backup runs.

```sh
farmctl backup --dry-run
farmctl backup --compress zstd --level 19    # override the configured default for this run
farmctl backup --no-compress                 # skip compression even if configured on
```

### `farmctl rebuild`

For after a full network wipe: reconnects every device on record (from
its last known SSH/ADB connection info), regenerates an appendable SSH
config, attempts to reconnect any network ADB targets, and writes a
`kubectl label node` script from the archived fingerprints — plus a
reachable-vs-missing report so you know what needs physical
re-onboarding versus what just needs a network to come back.

### `farmctl merge <source-dir> [<source-dir> ...]`

For restoring from multiple independent sources (e.g. two different
collectors that both backed up overlapping parts of the fleet):
re-derives each device's canonical identity from its own fingerprint data
(not from whichever folder name a source happened to use), de-duplicates
by actual content hash, and converges everything into the one canonical
local archive — without losing genuinely distinct history.

### `farmctl status`

Prints current config and a summary of the local archive (root path,
device count).

### `farmctl compressors`

Passthrough to `cloud-restore`'s compression availability check — see
what's actually installed on this machine.

## Where things live

- Device archive (auto-detected per platform):
  - Linux: `/home/Documents/persist_logs/`
  - Termux: `/data/data/com.termux/files/home/persist_logs/`

  Deliberately a fixed, non-per-user path — this is meant to work
  correctly even on a system running as root with no real user session,
  and to survive a rebuild regardless of which account runs the collector
  next time.
- `farmctl` config: `~/.config/farmctl/config.json` (genuinely per-user,
  unlike the archive above).

## Dependency structure, explicitly

```
autopath        <- standalone, zero dependencies
cloud-restore   <- standalone, zero dependencies (not even on autopath)
farmctl         <- requires BOTH of the above; contains only
                    device-fleet-specific logic (device_inventory.py)
```

If you're looking for reusable, project-agnostic path/backup logic for a
*different* project, use `autopath` and `cloud-restore` directly — this
package is the private consumer, not the thing to build on top of for
something unrelated to device fleet management.

## License

Private/internal (update this section before any public release —
`farmctl` contains domain-specific logic you may not want to publish even
if `autopath` and `cloud-restore` are released separately).
