#!/usr/bin/env bash
# Codex CLI (0.163 or newer) reports its state to Zellij itself, and the scanner reads the rest from Codex's own
# files, so nothing is installed into Codex. --uninstall (or --uninstall-hooks) strips the --codex-hook hooks older
# versions added to ~/.codex/hooks.json.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/zam-install-lib.sh"
hooks="${CODEX_HOME:-$HOME/.codex}/hooks.json"
# the events older versions hooked
events='["UserPromptSubmit","PreToolUse","PermissionRequest","PostToolUse","Stop","SessionEnd"]'

# our matcher groups by pattern, not exact path, so stale ones from a moved checkout are found too
defs='def isours: (.command // "") | contains("zam-scan.py") and contains("--codex-hook");
def ourhook: any(.hooks[]?; isours);
def strip: .hooks |= map(select(isours | not));'

uninstall= had='[]'
if [[ ${1:-} == --uninstall || ${1:-} == --uninstall-hooks ]]; then
  uninstall=1
  # events that already had an empty array before install (per the backup) keep it
  [[ -f "$hooks.zam.bak" ]] && had=$(jq -c '[.hooks // {} | to_entries[] | select(.value == []) | .key]' "$hooks.zam.bak" 2>/dev/null || echo '[]')
  [[ -f "$hooks" ]] && edit "$hooks" '
    reduce $events[] as $ev (.;
      if any(.hooks[$ev][]?; ourhook) then
        .hooks[$ev] |= map(if ourhook then (strip | select(.hooks | length > 0)) else . end) | if .hooks[$ev] == [] and ($had | index($ev) | not) then del(.hooks[$ev]) else . end
      else . end)'
  echo "old Codex hooks removed"
  exit 0
fi

echo "Codex needs no setup: it reports its state to Zellij itself"
