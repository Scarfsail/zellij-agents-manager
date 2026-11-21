#!/usr/bin/env bash
# Wraps Claude Code's status line in ~/.claude/settings.json (idempotent, keeps a .zam.bak of what it changes;
# --uninstall undoes it). The wrapper (zam-scan.py --claude-statusline) records each session's cost for the panel and
# chains to your own status line command, which gets the same input and whose output is still shown.
# Claude's state comes from Zellij itself, so no hooks are installed. --uninstall also strips the zam-hook.sh hooks
# older versions added; --uninstall-hooks strips only those.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/zam-install-lib.sh"
sl=$(jq -rn --arg s "$here/zam-scan.py" '"python3 \($s|@sh) --claude-statusline"')
settings="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
# the events older versions hooked
events='["PreToolUse","PostToolUse","PermissionRequest","Notification","UserPromptSubmit","Stop","SessionEnd"]'

# our hooks by pattern, not exact path, so stale ones from a moved checkout are removed too
defs='def isours: (.command // "") | contains("zam-hook.sh");
def ourhook: any(.hooks[]?; isours);'

uninstall= had='[]'
if [[ ${1:-} == --uninstall || ${1:-} == --uninstall-hooks ]]; then
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
  if [[ $1 == --uninstall-hooks ]]; then
    echo "old Claude hooks removed"
    exit 0
  fi
  [[ -f "$settings" ]] && sl_uninstall "$settings"
  echo "Claude status line and old hooks removed"
  exit 0
fi

mkdir -p "$(dirname "$settings")"
[[ -f "$settings" ]] || echo '{}' > "$settings"
sl_install "$settings"
echo "status line installed in $settings (backup: settings.json.zam.bak)"
