#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 TARGET --type ssh|adb [--timeout SECONDS]" >&2
  exit 2
fi

# This is a read-only check by convention: each enabled integration must
# define a preflight_argv that verifies its CLI context/connectivity/auth.
farmctl validate-adapters "$@"

# integrate --apply repeats preflight immediately before any configured
# onboarding commands, preventing a stale standalone validation from being
# the only gate.
exec farmctl integrate "$@" --apply
