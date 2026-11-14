#!/usr/bin/env bash
# Wires zam-scan.py into Codex CLI (idempotent, keeps a .zam.bak of what it changes; --uninstall undoes it):
# hooks in ~/.codex/hooks.json give "waiting for approval" (NEEDS YOU) and the current tool. Listing Codex agents,
# busy/idle, context and the usage line need none of this. Codex runs a new or changed hook only after you trust it
# in its /hooks view; restart running Codex agents afterwards.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/zam-install-lib.sh"
hook=$(jq -rn --arg s "$here/zam-scan.py" '"python3 \($s|@sh) --codex-hook"')
hooks="${CODEX_HOME:-$HOME/.codex}/hooks.json"
events='["UserPromptSubmit","PreToolUse","PermissionRequest","PostToolUse","Stop","SessionEnd"]'

# our matcher groups by pattern, not exact path, so stale ones from a moved checkout are found too
defs='def ourhook: any(.hooks[]?; (.command // "") | contains("zam-scan.py") and contains("--codex-hook"));'

uninstall= had='[]'
if [[ ${1:-} == --uninstall ]]; then
  uninstall=1
  # events that already had an empty array before install (per the backup) keep it
  [[ -f "$hooks.zam.bak" ]] && had=$(jq -c '[.hooks // {} | to_entries[] | select(.value == []) | .key]' "$hooks.zam.bak" 2>/dev/null || echo '[]')
  [[ -f "$hooks" ]] && edit "$hooks" '
    reduce $events[] as $ev (.;
      if any(.hooks[$ev][]?; ourhook) then
        .hooks[$ev] |= map(select(ourhook | not)) | if .hooks[$ev] == [] and ($had | index($ev) | not) then del(.hooks[$ev]) else . end
      else . end)'
  echo "Codex hooks removed"
  exit 0
fi

mkdir -p "$(dirname "$hooks")"
[[ -f "$hooks" ]] || echo '{"hooks":{}}' > "$hooks"
edit "$hooks" '
  {"matcher": "", "hooks": [{"type": "command", "command": $hook, "timeout": 5}]} as $ours |
  .hooks //= {} |
  reduce $events[] as $ev (.;
    .hooks[$ev] //= [] |
    .hooks[$ev] |= (([.[] | ourhook] | index(true)) as $i |
      if $i == null then . + [$ours]
      else [to_entries[] | select(.key == $i or (.value | ourhook | not)) | .value | if ourhook then $ours else . end] end))'
echo "Codex hooks in $hooks (backup: hooks.json.zam.bak). Trust them once in Codex's /hooks, then restart running Codex agents"
