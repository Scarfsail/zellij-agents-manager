#!/usr/bin/env bash
# Adds zam-hook.sh to ~/.claude/settings.json (idempotent, keeps a .bak).
# Only needed for the "NEEDS YOU" (permission prompt) state; everything else works without hooks.
set -euo pipefail
hook="$(cd "$(dirname "$0")" && pwd)/zam-hook.sh"
settings="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
[[ -f "$settings" ]] || echo '{}' > "$settings"
cp "$settings" "$settings.zam.bak"
tmp=$(mktemp)
jq --arg cmd "$hook" '
  .hooks //= {} |
  reduce ("PreToolUse","PostToolUse","PermissionRequest","Notification","UserPromptSubmit","Stop","SessionEnd") as $ev (.;
    .hooks[$ev] //= [] |
    if any(.hooks[$ev][]?.hooks[]?; .command == $cmd) then .
    else .hooks[$ev] += [{"matcher": "", "hooks": [{"type": "command", "command": $cmd, "timeout": 5}]}] end)
' "$settings" > "$tmp" && mv "$tmp" "$settings"
echo "hooks installed in $settings (backup: $settings.zam.bak)"
