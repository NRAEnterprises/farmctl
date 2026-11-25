"""Interactive terminal selector for locally available integration tools."""

import curses
import importlib.util
import shutil
from dataclasses import dataclass

from .integration_inventory import COMPONENTS, IntegrationStore

# Each component maps to one or more local client executables. Presence means
# the client is installed on this farmctl host; it does not prove a remote
# service is reachable or that credentials are valid.
COMMAND_PROBES = {
    "slurm": ("scontrol", "sinfo"),
    "nats-jetstream": ("nats",),
    "ray": ("ray",),
    "cilium": ("cilium",),
    "ansible": ("ansible", "ansible-playbook"),
    "nomad": ("nomad",),
    "netbox": ("netbox", "curl"),
    "kubernetes": ("kubectl",),
    "falco": ("falcoctl", "falco"),
    "keda": ("kubectl",),
    "argocd": ("argocd",),
    "tetragon": ("tetra", "kubectl"),
    "tailscale": ("tailscale",),
    "temporal": ("temporal",),
    "loki": ("logcli", "curl"),
    "tempo": ("tempo-cli", "curl"),
    "deepspeed": ("deepspeed",),
    "ipex-llm": ("ipex-llm",),
    "terraform": ("terraform",),
    "kafka": ("kafka-topics", "kafka-topics.sh"),
    "zeromq": ("zmqsend", "zmqrecv", "zpub", "zsub"),
    "rabbitmq": ("rabbitmqctl", "rabbitmqadmin"),
    "redpanda": ("rpk",),
    "redisstream": ("redis-cli",),
    "rubix": ("rubix",),
    "apollo": ("rover", "apollo"),
    "falcon": ("falconctl",),
    "victoriametrics": ("vmctl", "vmalert", "vmagent", "vmstorage"),
    "graphql": ("graphql", "curl"),
    "sql": ("psql", "mysql", "mariadb", "sqlite3"),
    "duckdb": ("duckdb",),
    "nubeio": ("nubeio", "curl"),
    "mqtt": ("mosquitto_pub", "mqtt"),
    "modbus": ("mbpoll", "modpoll"),
    "nginx": ("nginx",),
    "ngrok": ("ngrok",),
    "bacnet": ("bacwi", "bacrp", "bacrpm"),
    "grafana": ("grafana-cli", "grafana"),
    "boinc": ("boinccmd", "boinc_cmd"),
    "regex": ("rg", "grep"),
    "rsync": ("rsync",),
    "rclone": ("rclone",),
    "prometheus": ("promtool",),
    "hetzner": ("hcloud",),
    "equinix-metal": ("metal",),
    "ovh": ("ovh",),
    "ixp": ("ixp", "curl"),
    "arin": ("whois", "curl"),
    "ripe": ("whois", "curl"),
    "truenas": ("midclt", "curl"),
    "cloudflare": ("cloudflared", "wrangler"),
    "wireguard": ("wg",),
    "son-of-grid-engine": ("qconf", "qsub"),
}

# These integrations are often installed as Python modules rather than a
# standalone CLI. Module checks are local package-presence probes only.
MODULE_PROBES = {
    "deepspeed": ("deepspeed",),
    "ipex-llm": ("ipex_llm",),
    "zeromq": ("zmq",),
    "netbox": ("pynetbox",),
}


@dataclass(frozen=True)
class ToolPresence:
    available: bool
    found_commands: tuple[str, ...] = ()
    found_modules: tuple[str, ...] = ()

    @property
    def detected_by(self) -> str:
        found = self.found_commands + self.found_modules
        return ", ".join(found) if found else "not found"


def detect_tools(command_finder=shutil.which, module_finder=importlib.util.find_spec):
    """Return local CLI/module availability for every catalog component."""
    if set(COMMAND_PROBES) != set(COMPONENTS):
        missing = sorted(set(COMPONENTS) - set(COMMAND_PROBES))
        extra = sorted(set(COMMAND_PROBES) - set(COMPONENTS))
        raise RuntimeError(f"Probe catalog mismatch (missing={missing}, extra={extra})")

    result = {}
    for component in COMPONENTS:
        commands = tuple(command for command in COMMAND_PROBES[component]
                         if command_finder(command))
        modules = []
        for module in MODULE_PROBES.get(component, ()):
            try:
                if module_finder(module):
                    modules.append(module)
            except (ImportError, ValueError):
                pass
        result[component] = ToolPresence(bool(commands or modules), commands, tuple(modules))
    return result


def saved_selection(store: IntegrationStore, presence):
    """Load enabled state, masking unavailable components out of the selection."""
    selected = set()
    for component in COMPONENTS:
        settings = store.get_component(component)
        if (presence[component].available and settings
                and settings.get("enabled") is True):
            selected.add(component)
    return selected


def save_selection(store: IntegrationStore, selected, presence):
    """Persist active flags, forcing missing tools inactive while preserving config."""
    active = set(selected)
    invalid = active - set(COMPONENTS)
    if invalid:
        raise ValueError("Unknown integration component(s): " + ", ".join(sorted(invalid)))
    saved = {}
    for component in COMPONENTS:
        enabled = presence[component].available and component in active
        saved[component] = enabled
        store.set_component(component, enabled=enabled)
    return saved


def toggle_selection(selected, component, presence):
    """Toggle an available component; unavailable tools cannot be activated."""
    if component not in COMPONENTS:
        raise ValueError(f"Unknown integration component: {component}")
    result = set(selected)
    if not presence[component].available:
        result.discard(component)
    elif component in result:
        result.remove(component)
    else:
        result.add(component)
    return result


def _draw_menu(stdscr, selected, presence, adapter_ready, current_index, first_row):
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    title = "farmctl integration selection — arrows move, Space toggles, Enter saves, q cancels"
    stdscr.addnstr(0, 0, title, max(1, width - 1), curses.A_BOLD)
    stdscr.addnstr(1, 0, "Active tools are used by the normal integrate pipeline. Missing tools are dimmed and skipped.",
                    max(1, width - 1))
    list_top = 3
    visible_rows = max(1, height - list_top - 3)
    last_row = min(len(COMPONENTS), first_row + visible_rows)
    for index in range(first_row, last_row):
        component = COMPONENTS[index]
        found = presence[component]
        enabled = component in selected and found.available
        state = "ACTIVE" if enabled else "inactive" if found.available else "not found"
        marker = "[x]" if enabled else "[ ]" if found.available else "[-]"
        adapter = "adapter ready" if adapter_ready[component] else "adapter missing"
        line = f"{marker} {component:<22} {state:<10} {adapter:<15} {found.detected_by}"
        attr = curses.A_NORMAL if found.available else curses.A_DIM
        if index == current_index:
            attr |= curses.A_REVERSE
        stdscr.addnstr(list_top + index - first_row, 0, line, max(1, width - 1), attr)
    footer_y = max(list_top, height - 2)
    stdscr.addnstr(footer_y, 0,
                    "[*] saved active  [ ] installed/inactive  [-] unavailable and not expected",
                    max(1, width - 1), curses.A_BOLD)
    stdscr.refresh()


def _interactive_loop(stdscr, selected, presence, adapter_ready):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    current_index = 0
    first_row = 0
    while True:
        height, _ = stdscr.getmaxyx()
        visible_rows = max(1, height - 6)
        if current_index < first_row:
            first_row = current_index
        elif current_index >= first_row + visible_rows:
            first_row = current_index - visible_rows + 1
        _draw_menu(stdscr, selected, presence, adapter_ready, current_index, first_row)
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            current_index = (current_index - 1) % len(COMPONENTS)
        elif key in (curses.KEY_DOWN, ord("j")):
            current_index = (current_index + 1) % len(COMPONENTS)
        elif key == ord(" "):
            selected = toggle_selection(selected, COMPONENTS[current_index], presence)
        elif key in (10, 13, curses.KEY_ENTER):
            return selected, True
        elif key in (ord("q"), ord("Q"), 27):
            return selected, False


def run_integrations_menu(store: IntegrationStore | None = None):
    """Run the interactive selector and persist on Enter; q/Esc cancels."""
    store = store or IntegrationStore()
    presence = detect_tools()
    selected = saved_selection(store, presence)
    adapter_ready = {}
    for component in COMPONENTS:
        settings = store.get_component(component) or {}
        config = settings.get("config") or {}
        adapter_ready[component] = bool(config.get("preflight_argv") and config.get("onboard_argv"))
    selected, save = curses.wrapper(_interactive_loop, selected, presence, adapter_ready)
    if not save:
        return {"saved": False, "selected": selected, "presence": presence}
    saved = save_selection(store, selected, presence)
    return {"saved": True, "selected": {name for name, active in saved.items() if active},
            "presence": presence}
