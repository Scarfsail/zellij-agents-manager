#!/usr/bin/env bash
# Wires zam-scan.py into Cursor CLI (idempotent, keeps a .zam.bak of what it changes; --uninstall undoes it): the
# status line in ~/.cursor/cli-config.json gives context %, chaining to your own status line command if you have one.
# Restart running Cursor agents afterwards. Listing them and their working/idle state need none of this.
# --uninstall also strips the --cursor-hook hooks older versions added to ~/.cursor/hooks.json; --uninstall-hooks
# strips only those.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/zam-install-lib.sh"
sl=$(jq -rn --arg s "$here/zam-scan.py" '"python3 \($s|@sh) --cursor-statusline"')
hooks="$HOME/.cursor/hooks.json"
config="$HOME/.cursor/cli-config.json"
# the events older versions hooked
events='["beforeSubmitPrompt","preToolUse","postToolUse","postToolUseFailure","stop","sessionEnd"]'

# our entries by pattern, not exact path, so stale ones from a moved checkout are found too
defs='def ourhook: (.command // "") | contains("zam-scan.py") and contains("--cursor-hook");'

uninstall= had='[]'
if [[ ${1:-} == --uninstall || ${1:-} == --uninstall-hooks ]]; then
  uninstall=1
  # events that already had an empty array before install (per the backup) keep it
  [[ -f "$hooks.zam.bak" ]] && had=$(jq -c '[.hooks // {} | to_entries[] | select(.value == []) | .key]' "$hooks.zam.bak" 2>/dev/null || echo '[]')
  [[ -f "$hooks" ]] && edit "$hooks" '
    reduce $events[] as $ev (.;
      if any(.hooks[$ev][]?; ourhook) then
        .hooks[$ev] |= map(select(ourhook | not)) | if .hooks[$ev] == [] and ($had | index($ev) | not) then del(.hooks[$ev]) else . end
      else . end)'
  if [[ $1 == --uninstall-hooks ]]; then
    echo "old Cursor hooks removed"
    exit 0
  fi
  [[ -f "$config" ]] && sl_uninstall "$config"
  echo "Cursor status line and old hooks removed"
  exit 0
fi

mkdir -p "$HOME/.cursor"
[[ -f "$config" ]] || echo '{}' > "$config"
sl_install "$config"
echo "Cursor status line in $config (backup: cli-config.json.zam.bak); restart running Cursor agents"
