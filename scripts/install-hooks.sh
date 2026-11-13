#!/usr/bin/env bash
# Adds zam-hook.sh to ~/.claude/settings.json and wraps its status line (idempotent, keeps a .bak).
# The hooks are only needed for the "NEEDS YOU" (permission prompt) state; everything else works without them.
# The status line wrapper (zam-scan.py --claude-statusline) records each session's cost for the panel and
# chains to your own status line command, which gets the same input and whose output is still shown.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
hook="$here/zam-hook.sh"
sl=$(jq -rn --arg s "$here/zam-scan.py" '"python3 \($s|@sh) --claude-statusline"')
settings="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
[[ -f "$settings" ]] || echo '{}' > "$settings"
cp "$settings" "$settings.zam.bak"
tmp=$(mktemp)
# our status line by pattern, not exact path, so one from a moved checkout is found too;
# slrest: what follows our status line prefix ("" or " -- <quoted previous command>")
jq --arg cmd "$hook" --arg sl "$sl" '
  def oursl: contains("zam-scan.py") and contains("--claude-statusline");
  def slrest: split("--claude-statusline")[1:] | join("--claude-statusline");
  .hooks //= {} |
  reduce ("PreToolUse","PostToolUse","PermissionRequest","Notification","UserPromptSubmit","Stop","SessionEnd") as $ev (.;
    .hooks[$ev] //= [] |
    if any(.hooks[$ev][]?.hooks[]?; .command == $cmd) then .
    else .hooks[$ev] += [{"matcher": "", "hooks": [{"type": "command", "command": $cmd, "timeout": 5}]}] end) |
  # an already wrapped status line: only the prefix is rewritten, the chained part stays
  if (.statusLine.command // "") | oursl then .statusLine.command |= $sl + slrest
  elif (.statusLine.command // "") == "" then .statusLine.type //= "command" | .statusLine.command = $sl
  else .statusLine.command |= "\($sl) -- \(@sh)" end
' "$settings" > "$tmp" && mv "$tmp" "$settings"
echo "hooks and status line installed in $settings (backup: $settings.zam.bak)"
