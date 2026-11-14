# shellcheck shell=bash
# Shared by the install-*.sh scripts (sourced). edit() reads the caller's $hook, $events, $had, $defs, $uninstall
# and, if set, $sl and $sladd: they become jq's $hook, $events, $had, $sl, $sladd and $q (a single quote), and $defs
# precedes the filter.

edit() { # file, jq filter: replace the file atomically (keeping its mode); on first install, back up the original
  local f=$1 tmp
  tmp=$(mktemp "$(dirname "$f")/.$(basename "$f").XXXXXX")
  jq --arg hook "${hook:-}" --arg sl "${sl:-}" --arg q "'" --argjson events "${events:-[]}" --argjson had "${had:-[]}" \
    --argjson sladd "${sladd:-"{}"}" "${defs:-} $2" "$f" > "$tmp"
  if ! cmp -s "$tmp" "$f"; then
    [[ $uninstall || -e "$f.zam.bak" ]] || cp "$f" "$f.zam.bak"
    chmod --reference="$f" "$tmp"
    mv -f "$tmp" "$f"
  else
    rm "$tmp"
  fi
}

# The status line in a settings file: .statusLine.command becomes $sl (python3 '<scan>' --<kind>-statusline),
# chaining to the previous command as "$sl -- <@sh-quoted previous>". Our command is found by pattern, not exact
# path, so a wrapper from a moved checkout is found too; slrest is what follows its flag ("" or " -- <quoted>").
# $sladd: what a status line we add (none before) gets beside the command; uninstall drops it again.
sldefs='def slflag: $sl | split(" ") | last;
  def oursl: contains("zam-scan.py") and contains(slflag);
  def slrest: .[index(slflag) + (slflag | length):];'

sl_install() { # settings file
  local defs="${defs:-} $sldefs"
  # an already wrapped status line: only the prefix is rewritten, the chained part stays
  edit "$1" '
    if (.statusLine.command // "") | oursl then .statusLine.command |= $sl + slrest
    elif (.statusLine.command // "") == "" then .statusLine.type //= "command" | .statusLine.command = $sl | .statusLine += $sladd
    else .statusLine.command |= "\($sl) -- \(@sh)" end'
}

sl_uninstall() { # settings file
  local defs="${defs:-} $sldefs"
  # our wrapper quoted the previous command with @sh: undo that, or drop the status line we added
  edit "$1" '
    (.statusLine.command // "") as $c |
    if ($c | oursl | not) then .
    elif ($c | slrest) == "" then
      del(.statusLine.command) | .statusLine |= with_entries(select(.key as $k | $sladd | has($k) | not)) |
      if .statusLine == {} or .statusLine == {type: "command"} then del(.statusLine) else . end
    elif ($c | slrest) | startswith(" -- ") then
      .statusLine.command = ($c | slrest | ltrimstr(" -- ") | .[1:-1] | split($q + "\\" + $q + $q) | join($q))
    else . end'
}
