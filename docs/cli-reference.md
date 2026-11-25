# farmctl CLI reference

This manual describes the command-line interface in the current `main` branch. Run `farmctl --help` or `farmctl <command> --help` for the installed version's argparse output. Unless otherwise stated, paths and configuration are resolved by the farmctl process running the command.

## Safety and conventions

- `--type` is required for commands that contact one node: choose `ssh` for an SSH host or `adb` for an ADB serial.
- Integration commands execute on the machine running farmctl. They inherit that user's environment and credentials; SSH adapters may then execute commands on the target.
- Integration and teardown argv are JSON arrays, not shell strings. farmctl does not run them through a shell, interpolate shell syntax, or infer credentials.
- Do not put passwords, tokens, private keys, or other secrets in JSON settings or event data. Use the tool's normal credential store/environment.
- `integrate` and `purge` are plan-only by default. Their `--apply` modes may mutate external systems. Review the printed commands and the external tool context before applying.
- Farmctl cannot guarantee that a user-supplied preflight command is read-only. Configure probes accordingly.

## Global help

```text
farmctl --help
farmctl <command> --help
```

The installed CLI entry point is declared by the Python package. The project also provides `install.sh`, which installs the separately maintained `autopath` and `cloud-restore` prerequisites before farmctl.

## Integration settings and event commands

The component catalog includes Slurm, NATS JetStream, Ray, Cilium, Ansible, Nomad, NetBox, Kubernetes, Falco, KEDA, Argo CD, Tetragon, Tailscale, Temporal, Loki, Tempo, DeepSpeed, IPEX-LLM, Terraform, Kafka, ZeroMQ, RabbitMQ, Redpanda, Redis Streams, Rubix, Apollo, Falcon, VictoriaMetrics, GraphQL, SQL, DuckDB, NubeIO, MQTT, Modbus, NGINX, ngrok, BACnet, Grafana, BOINC, regex, rsync, rclone, Prometheus, Hetzner, Equinix Metal, OVH, IXP, ARIN, RIPE, TrueNAS, Cloudflare, WireGuard, and Son of Grid Engine. The canonical spellings accepted by the CLI are shown by `farmctl integration set --help`.

### `farmctl integration set <component>`

Set an integration's enabled state and/or JSON settings.

```text
farmctl integration set COMPONENT [--enable | --disable]
  [--switches-json JSON_OBJECT] [--config-json JSON_OBJECT]
```

`--enable` and `--disable` are mutually exclusive. If omitted, the enabled state is unchanged. `--switches-json` stores tool-specific desired switches. `--config-json` stores adapter configuration; setting it replaces the component's existing config object. Settings are saved beneath `persist_logs/integrations/<component>/settings.json` and included in the configured archive backup.

Example:

```sh
farmctl integration set kubernetes --enable \
  --switches-json '{"label_node":true}' \
  --config-json '{"preflight_argv":["kubectl","get","node","{hostname}","-o","name"],"onboard_argv":["kubectl","label","node","{hostname}","farmctl.io/managed=true","--overwrite"],"teardown_argv":["kubectl","label","node","{hostname}","farmctl.io/managed-"]}'
```

The teardown example removes only the sample farmctl label; it does not delete the Kubernetes Node object. Always adapt commands to the exact resources the onboarding adapter created.

### `farmctl integration show [component]`

Show saved settings for one component or all components as JSON. No mutation is performed.

```sh
farmctl integration show
farmctl integration show slurm
```

### `farmctl integration log <component> <event>`

Append an operator-supplied event to the component's JSON Lines event log.

```text
farmctl integration log COMPONENT EVENT [--data-json JSON_OBJECT]
```

Event names are limited to 1–120 ASCII letters, digits, period, underscore, colon, slash, and hyphen. `--data-json` must be a JSON object. Example:

```sh
farmctl integration log slurm job.submitted \
  --data-json '{"job_id":"4821","node":"worker-02","state":"PENDING"}'
```

### `farmctl integration events <component>`

Print recent events for one component.

```text
farmctl integration events COMPONENT [--limit INTEGER]
```

`--limit` defaults to 50 and must be at least 1.

## Node onboarding and preflight

### `farmctl integrate <target>`

Fingerprint a node and build an ordered plan through the integration catalog. The default is plan-only; only configured enabled adapters are candidates for execution.

```text
farmctl integrate TARGET --type {ssh,adb} [--apply] [--timeout SECONDS]
```

- `TARGET`: SSH host or ADB serial.
- `--type`: required connection mode.
- `--apply`: persist the inventory snapshot and execute each configured adapter sequentially. Without it, no onboarding action or snapshot write occurs.
- `--timeout`: per-adapter timeout, default 120 seconds.

For apply, every enabled adapter must have both a valid `preflight_argv` and `onboard_argv`, all required executables must exist, and all preflights must pass before any onboarding action runs. This is not a transaction: if a later adapter fails, earlier actions may already have succeeded. The inventory snapshot and per-tool `node.onboarding` events are stored locally.

Supported placeholders include `{hostname}`, `{device_id}`, `{manufacturer}`, `{model}`, `{device_type}`, `{architecture}`, `{kernel_release}`, `{cpu_threads}`, `{memory_mb}`, `{inventory_json}`, and `{loki_push_json}`. Placeholders are expanded inside individual argv elements. No shell quoting or command substitution is performed.

Example:

```sh
farmctl integrate worker-02 --type ssh              # plan only
farmctl integrate worker-02 --type ssh --apply      # explicit apply
```

### `farmctl validate-adapters <target>`

Run configured preflight argv arrays without running onboarding actions.

```text
farmctl validate-adapters TARGET --type {ssh,adb}
  [--timeout SECONDS] [--component COMPONENT ...]
```

- `--timeout`: maximum preflight wait in seconds, default 30.
- Repeat `--component` to select a subset. If omitted, all catalog components are considered.

This command fingerprints the target, runs enabled configured preflights, and reports status without printing captured command output. Use `--component` for quick probes of key tools. A passing probe confirms only the condition actually tested; for example, a read-only status check does not prove that a later write operation is authorized.

```sh
farmctl validate-adapters worker-02 --type ssh \
  --component tailscale --component kubernetes --component prometheus
```

The equivalent wrapper `scripts/validate-adapters.sh` accepts and forwards the same target and flags. `scripts/integrate-with-preflight.sh` runs validation and, if it succeeds, invokes apply; apply repeats the probes immediately before changes.

## Node teardown and disaster recovery

### `farmctl purge <target>`

Build a plan to reverse recorded onboarding actions for a node, and, after explicit confirmation, execute configured teardown adapters before removing that node's local snapshot directory.

```text
farmctl purge TARGET --type {ssh,adb} [--apply]
  [--confirm-device-id DEVICE_ID] [--timeout SECONDS]
```

- `--type`: required connection mode.
- Default mode is plan-only: it fingerprints the node, identifies tools with an attempted onboarding event for its device ID, and prints the required preflight and teardown argv arrays. No external command is run and no local data is removed.
- `--apply`: run the destructive workflow. For every recorded attempted integration, its current settings must include both `preflight_argv` and `teardown_argv`; each executable must exist. All purge preflights run before any teardown. A missing or invalid adapter blocks the entire teardown phase.
- `--confirm-device-id`: required with `--apply`. Copy the exact stable ID printed by the reviewed plan. A mismatch aborts before any external action.
- `--timeout`: per teardown timeout, default 120 seconds. Preflights are capped at 30 seconds.

Example two-step workflow:

```sh
farmctl purge worker-02 --type ssh
# Inspect the exact planned preflights/teardowns and copied device ID.
farmctl purge worker-02 --type ssh --apply --confirm-device-id 0123456789abcdef
```

Purge does **not** invent deletion commands for the 53 catalog entries. Configure each teardown to reverse only the specific resource created by that component's onboarding adapter. Generic integration configuration is global, so farmctl never deletes it. Successful/failed integration event history is retained, with `node.purged` events for successful component teardowns; a global `persist_logs/integrations/purge_audit.jsonl` records purge outcomes. Only the matching `persist_logs/<device_id>/` snapshot directory is removed, and only after every configured teardown succeeds. If an external teardown fails, earlier external teardowns may already have succeeded; the local snapshot is retained and the audit record reports the partial outcome. Reconcile the services before retrying. Back up the archive before production purge operations.

## Device inventory and backup commands

### `farmctl scan <target>`

```text
farmctl scan TARGET --type {ssh,adb}
```

Fingerprint a reachable node and write its taxonomy archive record. An unknown/unreachable host is not archived.

### `farmctl backup`

```text
farmctl backup [--project PROJECT] [--mirror] [--dry-run]
  [--compress | --no-compress] [--algorithm ALGORITHM] [--level INTEGER]
  [--no-autopath-registry]
```

- `--project`: override the configured project namespace.
- `--mirror`: for rsync, also delete remote files absent locally; use with care.
- `--dry-run`: ask the backend to preview changes.
- `--compress` / `--no-compress`: override the configured compression default for this run.
- `--algorithm`: choose a compression algorithm; with no value, the configured/automatic choice is used.
- `--level`: compression level for algorithms that support it.
- `--no-autopath-registry`: skip the optional autopath registry backup.

### `farmctl restore`

```text
farmctl restore [--project PROJECT] [--dry-run] [--fail-loud]
  [--no-autopath-registry]
```

`--project` overrides the configured namespace. `--dry-run` previews restore. The CLI's `--fail-loud` option defaults to enabled; restore detects the remote compression manifest automatically. `--no-autopath-registry` skips the optional secondary registry restore.

### `farmctl configure`

Interactively configure the backup backend (`rsync` or `rclone`), remote, project namespace, and default compression settings. It accepts no command-line flags.

### `farmctl rebuild`

Rebuild local SSH/Kubernetes artifacts from archived inventory. Takes no arguments or flags.

### `farmctl merge <source-dir> [<source-dir> ...]`

Converge one or more restored archive trees into the canonical local archive.

```text
farmctl merge SOURCE_DIR [SOURCE_DIR ...] [--dry-run]
```

`--dry-run` previews without applying the merge.

### `farmctl status`

Show current farmctl configuration and local archive location/device count. Takes no flags.

### `farmctl compressors`

List available compression algorithms and the automatic recommendation. Takes no flags.

## Data locations and logs

Device snapshots, integration `settings.json`, component `events.jsonl`, and `purge_audit.jsonl` are beneath the archive's `persist_logs/` tree. The archive is part of the existing backup/restore flow. User configuration lives under `~/.config/farmctl/`. Never publish archive or event files without reviewing them for sensitive hostnames, inventory, and service metadata.
