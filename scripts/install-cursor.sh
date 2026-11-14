#!/usr/bin/env bash
# Wires zam-scan.py into Cursor CLI (idempotent, keeps a .zam.bak of what it changes; --uninstall undoes it):
# hooks in ~/.cursor/hooks.json give exact busy/idle and the current tool, the status line in
# ~/.cursor/cli-config.json gives context %, chaining to your own status line command if you have one.
# Restart running Cursor agents afterwards. Listing them needs none of this.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/zam-install-lib.sh"
scan="$here/zam-scan.py"
hook=$(jq -rn --arg s "$scan" '"python3 \($s|@sh) --cursor-hook"')
sl=$(jq -rn --arg s "$scan" '"python3 \($s|@sh) --cursor-statusline"')
hooks="$HOME/.cursor/hooks.json"
config="$HOME/.cursor/cli-config.json"
events='["beforeSubmitPrompt","preToolUse","postToolUse","postToolUseFailure","stop","sessionEnd"]'

# our entries by pattern, not exact path, so stale ones from a moved checkout are found too
defs='def ourhook: (.command // "") | contains("zam-scan.py") and contains("--cursor-hook");'

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
  [[ -f "$config" ]] && sl_uninstall "$config"
  echo "Cursor hooks and status line removed"
  exit 0
fi

mkdir -p "$HOME/.cursor"
[[ -f "$hooks" ]] || echo '{"version":1,"hooks":{}}' > "$hooks"
[[ -f "$config" ]] || echo '{}' > "$config"
edit "$hooks" '
  .hooks //= {} |
  reduce $events[] as $ev (.;
    .hooks[$ev] //= [] |
    .hooks[$ev] |= (([.[] | ourhook] | index(true)) as $i |
      if $i == null then . + [{"command": $hook}]
      else [to_entries[] | select(.key == $i or (.value | ourhook | not)) | .value | if ourhook then .command = $hook else . end] end))'
sl_install "$config"
echo "Cursor hooks in $hooks, status line in $config (backups: *.zam.bak); restart running Cursor agents"
