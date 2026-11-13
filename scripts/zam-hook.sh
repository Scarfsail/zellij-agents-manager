#!/usr/bin/env bash
# Claude Code hook: record the last lifecycle event per session so the
# scanner can tell "waiting for permission" apart from "busy".
# Wire it to PreToolUse, PostToolUse, PermissionRequest, Notification,
# UserPromptSubmit, Stop and SessionEnd (see install-hooks.sh). SessionEnd also drops
# the status line record (<sid>.status.json, from zam-scan.py --claude-statusline).
dir="${XDG_CACHE_HOME:-$HOME/.cache}/zellij-agents/claude"
mkdir -p "$dir" || exit 0
input=$(cat)
sid=$(jq -r '.session_id // empty' <<<"$input" 2>/dev/null)
[[ -z "$sid" || "$sid" == */* ]] && exit 0
if [[ "$(jq -r '.hook_event_name' <<<"$input")" == "SessionEnd" ]]; then
  rm -f "$dir/$sid.json" "$dir/$sid.status.json" "$dir/../seen/$sid"
  exit 0
fi
jq -c --argjson ts "$(date +%s%3N)" '{
  event: .hook_event_name,
  tool: (.tool_name // null),
  notification_type: (.notification_type // null),
  message: (.message // null),
  ts: $ts
}' <<<"$input" > "$dir/.$sid.tmp" 2>/dev/null && mv -f "$dir/.$sid.tmp" "$dir/$sid.json"
exit 0
