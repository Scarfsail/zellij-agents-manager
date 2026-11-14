#!/usr/bin/env bash
# Interactive, idempotent setup: shows what is installed for Zellij and every detected agent, and installs, changes or
# removes any of it from an arrow-key menu; re-run it to change preferences. --status prints the overview and exits.
here="$(cd "$(dirname "$0")" && pwd)"
exec python3 -I "$here/scripts/zam-setup.py" "$@"
