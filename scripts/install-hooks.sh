#!/usr/bin/env bash
# Adds zam-hook.sh to ~/.claude/settings.json and wraps its status line (idempotent, keeps a .zam.bak of what it
# changes; --uninstall undoes it).
# The hooks are only needed for the "NEEDS YOU" (permission prompt) state; everything else works without them.
# The status line wrapper (zam-scan.py --claude-statusline) records each session's cost for the panel and
# chains to your own status line command, which gets the same input and whose output is still shown.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/zam-install-lib.sh"
hook="$here/zam-hook.sh"
sl=$(jq -rn --arg s "$here/zam-scan.py" '"python3 \($s|@sh) --claude-statusline"')
settings="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
events='["PreToolUse","PostToolUse","PermissionRequest","Notification","UserPromptSubmit","Stop","SessionEnd"]'

# our hooks by pattern, not exact path, so stale ones from a moved checkout are removed too
defs='def isours: (.command // "") | contains("zam-hook.sh");
def ourhook: any(.hooks[]?; isours);'

uninstall= had='[]'
if [[ ${1:-} == --uninstall ]]; then
  uninstall=1
  # events that already had an empty array before install (per the backup) keep it, and so does an empty "hooks"
  hadhooks=false
  if [[ -f "$settings.zam.bak" ]]; then
    had=$(jq -c '[.hooks // {} | to_entries[] | select(.value == []) | .key]' "$settings.zam.bak" 2>/dev/null || echo '[]')
    hadhooks=$(jq 'has("hooks")' "$settings.zam.bak" 2>/dev/null || echo false)
  fi
  defs="$defs def hadhooks: $hadhooks;"
  [[ -f "$settings" ]] && edit "$settings" '
    reduce $events[] as $ev (.;
      if any(.hooks[$ev][]?; ourhook) then
        .hooks[$ev] |= map(if ourhook then (.hooks |= map(select(isours | not)) | select(.hooks | length > 0)) else . end) | if .hooks[$ev] == [] and ($had | index($ev) | not) then del(.hooks[$ev]) else . end
      else . end) |
    if .hooks == {} and (hadhooks | not) then del(.hooks) else . end'
  [[ -f "$settings" ]] && sl_uninstall "$settings"
  echo "Claude hooks and status line removed"
  exit 0
fi

mkdir -p "$(dirname "$settings")"
[[ -f "$settings" ]] || echo '{}' > "$settings"
# our hooks from another checkout are dropped first (their groups too, once empty), so a reinstall replaces them
edit "$settings" '
  def stale: isours and .command != $hook;
  .hooks //= {} |
  reduce $events[] as $ev (.;
    .hooks[$ev] //= [] |
    .hooks[$ev] |= map(if any(.hooks[]?; stale) then (.hooks |= map(select(stale | not)) | select(.hooks | length > 0)) else . end) |
    if any(.hooks[$ev][]?.hooks[]?; .command == $hook) then .
    else .hooks[$ev] += [{"matcher": "", "hooks": [{"type": "command", "command": $hook, "timeout": 5}]}] end)'
sl_install "$settings"
echo "hooks and status line installed in $settings (backup: settings.json.zam.bak)"
