# shellcheck shell=bash
# Shared by the install-*.sh scripts (sourced). edit() reads the caller's $hook, $events, $had, $defs, $uninstall
# and, if set, $sl: they become jq's $hook, $events, $had, $sl and $q (a single quote), and $defs precedes the filter.

edit() { # file, jq filter: replace the file atomically (keeping its mode); on first install, back up the original
  local f=$1 tmp
  tmp=$(mktemp "$(dirname "$f")/.$(basename "$f").XXXXXX")
  jq --arg hook "$hook" --arg sl "${sl:-}" --arg q "'" --argjson events "$events" --argjson had "$had" "$defs $2" "$f" > "$tmp"
  if ! cmp -s "$tmp" "$f"; then
    [[ $uninstall || -e "$f.zam.bak" ]] || cp "$f" "$f.zam.bak"
    chmod --reference="$f" "$tmp"
    mv -f "$tmp" "$f"
  else
    rm "$tmp"
  fi
}
