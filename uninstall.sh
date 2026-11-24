#!/usr/bin/env bash
set -euo pipefail

case ${1:-} in
  -h|--help) echo "Usage: ./uninstall.sh"; exit 0 ;;
  '') ;;
  *) echo "Usage: ./uninstall.sh" >&2; exit 2 ;;
esac

here="$(cd "$(dirname "$0")" && pwd)"
failed=0
for script in install.sh install-hooks.sh install-codex.sh install-cursor.sh install-agy.sh; do
  if ! "$here/scripts/$script" --uninstall; then
    echo "Uninstall failed in scripts/$script" >&2
    failed=1
  fi
done
(( failed == 0 )) || exit 1

cache="${XDG_CACHE_HOME:-$HOME/.cache}"
python3 -I - "$cache/zellij/permissions.kdl" <<'PY'
import os
from pathlib import Path
import re
import sys
import tempfile

path = Path(sys.argv[1]).resolve()
try:
    original = path.read_bytes()
except FileNotFoundError:
    sys.exit(0)

# Zellij stores one quoted plugin path per KDL block, including paths from older checkouts.
updated = re.sub(
    rb'(?m)^[ \t]*"(?:[^"\r\n]*/)?zellij-agents\.wasm"[ \t]*\{[ \t]*\r?\n'
    rb'(?:[ \t]*\w*[ \t]*\r?\n)*[ \t]*\}[ \t]*(?:\r?\n|$)',
    b'', original,
)
if updated != original:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temp = Path(output.name)
        try:
            output.write(updated)
            output.close()
            temp.chmod(path.stat().st_mode & 0o777)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
PY

cfg="${ZELLIJ_CONFIG_DIR:-$HOME/.config/zellij}/config.kdl"
if cfg_real=$(readlink -f "$cfg"); then
  rm -f -- "$cfg_real.zam.bak"
fi
rm -f -- "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json.zam.bak" \
  "${CODEX_HOME:-$HOME/.codex}/hooks.json.zam.bak" \
  "$HOME/.cursor/hooks.json.zam.bak" "$HOME/.cursor/cli-config.json.zam.bak" \
  "$HOME/.gemini/antigravity-cli/settings.json.zam.bak"
rm -rf -- "$cache/zellij-agents"

echo "Zellij Agents Manager removed. Restart running agents and start a fresh Zellij session."
