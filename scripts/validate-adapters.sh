#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 TARGET --type ssh|adb [--component NAME ...] [--timeout SECONDS]" >&2
  exit 2
fi

exec farmctl validate-adapters "$@"
