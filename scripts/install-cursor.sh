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

# our entries by pattern, not exact path, so stale ones from a moved checkout are found too;
# slrest: what follows our status line prefix ("" or " -- <quoted previous command>")
defs='def ourhook: (.command // "") | contains("zam-scan.py") and contains("--cursor-hook");
  def oursl: contains("zam-scan.py") and contains("--cursor-statusline");
  def slrest: .[index("--cursor-statusline") + 19:];'

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
  # our wrapper quoted the previous command with @sh: undo that, or drop the status line command we added
  [[ -f "$config" ]] && edit "$config" '
    (.statusLine.command // "") as $c |
    if ($c | oursl | not) then .
    elif ($c | slrest) == "" then
      del(.statusLine.command) | if .statusLine == {} or .statusLine == {type: "command"} then del(.statusLine) else . end
    elif ($c | slrest) | startswith(" -- ") then
      .statusLine.command = ($c | slrest | ltrimstr(" -- ") | .[1:-1] | split($q + "\\" + $q + $q) | join($q))
    else . end'
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
# an already wrapped status line (maybe from a moved checkout): only the prefix is rewritten, the chained part stays
edit "$config" '
  if (.statusLine.command // "") | oursl then .statusLine.command |= $sl + slrest
  elif (.statusLine.command // "") == "" then .statusLine.type //= "command" | .statusLine.command = $sl
  else .statusLine.command |= "\($sl) -- \(@sh)" end'
echo "Cursor hooks in $hooks, status line in $config (backups: *.zam.bak); restart running Cursor agents"
