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
defs='def isours: (.command // "") | contains("zam-scan.py") and contains("--codex-hook");
def ourhook: any(.hooks[]?; isours);
def strip: .hooks |= map(select(isours | not));'

uninstall= had='[]'
if [[ ${1:-} == --uninstall ]]; then
  uninstall=1
  # events that already had an empty array before install (per the backup) keep it
  [[ -f "$hooks.zam.bak" ]] && had=$(jq -c '[.hooks // {} | to_entries[] | select(.value == []) | .key]' "$hooks.zam.bak" 2>/dev/null || echo '[]')
  [[ -f "$hooks" ]] && edit "$hooks" '
    reduce $events[] as $ev (.;
      if any(.hooks[$ev][]?; ourhook) then
        .hooks[$ev] |= map(if ourhook then (strip | select(.hooks | length > 0)) else . end) | if .hooks[$ev] == [] and ($had | index($ev) | not) then del(.hooks[$ev]) else . end
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
      else [to_entries[] | .key as $k | .value as $g |
        if $k == $i then $g | .hooks |= (reduce .[] as $h ({l: [], s: false};
          if ($h | isours) then (if .s then . else .l += [$h | .command = $hook] | .s = true end) else .l += [$h] end) | .l)
        elif ($g | ourhook) then $g | strip | select(.hooks | length > 0)
        else $g end] end))'
echo "Codex hooks in $hooks (backup: hooks.json.zam.bak). Trust them once in Codex's /hooks, then restart running Codex agents"
