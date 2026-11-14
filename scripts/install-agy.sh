#!/usr/bin/env bash
# Wires zam-scan.py into agy, the Antigravity CLI (idempotent, keeps a .zam.bak of what it changes; --uninstall
# undoes it): the status line in ~/.gemini/antigravity-cli/settings.json gives busy/idle, NEEDS YOU, title, model,
# context %, COST and the usage line, chaining to your own status line command if you have one. When you have none,
# it is stacked below agy's built-in one. Restart running agy agents afterwards. Listing them needs none of this.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/zam-install-lib.sh"
sl=$(jq -rn --arg s "$here/zam-scan.py" '"python3 \($s|@sh) --agy-statusline"')
settings="$HOME/.gemini/antigravity-cli/settings.json"
sladd='{"stack_with_default": true}'

uninstall=
if [[ ${1:-} == --uninstall ]]; then
  uninstall=1
  [[ -f "$settings" ]] && sl_uninstall "$settings"
  echo "agy status line removed"
  exit 0
fi

mkdir -p "$(dirname "$settings")"
[[ -f "$settings" ]] || echo '{}' > "$settings"
sl_install "$settings"
echo "agy status line in $settings (backup: settings.json.zam.bak); restart running agy agents"
