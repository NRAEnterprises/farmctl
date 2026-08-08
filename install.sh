#!/bin/sh
# farmctl installer — works on Linux and Termux (Android).
#
# Pure-Python, no compiled extensions — same reasoning as autopath's
# and cloud_restore's own installers. Installs both required
# prerequisites first (autopath, cloud_restore), then farmctl itself.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/<YOUR_GH_USER>/farmctl/main/install.sh | sh

set -eu

REPO_URL="${FARMCTL_REPO_URL:-https://github.com/CHANGE_ME/farmctl/archive/refs/heads/main.tar.gz}"
AUTOPATH_INSTALL_URL="${AUTOPATH_INSTALL_URL:-https://raw.githubusercontent.com/CHANGE_ME/autopath/main/install.sh}"
CLOUD_RESTORE_INSTALL_URL="${CLOUD_RESTORE_INSTALL_URL:-https://raw.githubusercontent.com/CHANGE_ME/cloud_restore/main/install.sh}"

have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

download() {
    url="$1"
    output="$2"
    if have_cmd curl; then
        curl -fsSL "$url" -o "$output"
    elif have_cmd wget; then
        wget -qO "$output" "$url"
    else
        echo "curl or wget is required to install farmctl." >&2
        exit 1
    fi
}

fetch_and_run() {
    url="$1"
    if have_cmd curl; then
        curl -fsSL "$url" | sh
    elif have_cmd wget; then
        wget -qO- "$url" | sh
    else
        echo "curl or wget is required." >&2
        exit 1
    fi
}

find_python() {
    for candidate in python3 python; do
        if have_cmd "$candidate"; then
            echo "$candidate"
            return 0
        fi
    done
    echo "No python3 interpreter found on PATH." >&2
    exit 1
}

main() {
    echo "Installing required prerequisites..."
    echo
    echo "-- autopath --"
    fetch_and_run "$AUTOPATH_INSTALL_URL"
    echo
    echo "-- cloud_restore --"
    fetch_and_run "$CLOUD_RESTORE_INSTALL_URL"
    echo

    PY="$(find_python)"
    echo "Using interpreter: $($PY --version 2>&1) ($PY)"

    TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/farmctl-install.XXXXXX")"
    trap 'rm -rf "$TMP_DIR"' EXIT INT TERM

    archive="$TMP_DIR/farmctl.tar.gz"
    download "$REPO_URL" "$archive"

    tar -xzf "$archive" -C "$TMP_DIR"
    src_dir="$(find "$TMP_DIR" -maxdepth 1 -type d -name 'farmctl-*' | head -n 1)"
    if [ -z "$src_dir" ]; then
        echo "Could not locate extracted package source directory." >&2
        exit 1
    fi

    if "$PY" -m pip install --user "$src_dir" 2>/tmp/farmctl-pip-err.log; then
        :
    else
        echo "Standard install failed, retrying with --break-system-packages..." >&2
        "$PY" -m pip install --user --break-system-packages "$src_dir"
    fi

    echo
    echo "farmctl installed."
    echo
    echo "Next steps:"
    echo "  farmctl configure     # set backend + remote, once"
    echo "  farmctl scan <target> --type ssh|adb"
    echo "  farmctl backup"
}

main "$@"
