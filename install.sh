#!/usr/bin/env bash
# Installs the agents bar into every new zellij session:
#   1. ~/.config/zellij/layouts/agents.kdl       = zellij's default layout + agents bar under the tab bar
#   2. ~/.config/zellij/layouts/agents.swap.kdl  = zellij's default swap layouts + agents bar (Alt [ / Alt ] keep it)
#   3. default_layout "agents" in config.kdl (backup: config.kdl.zam.bak)
#   4. <mod>+1..9 (jump to agent n), <mod>+0 (toggle the agents panel, like clicking the bar's header) and <mod>+left/right (previous/next agent, wrapping) in config.kdl
#      (lines tagged "// zellij-agents"; re-running replaces them)
# The layout points at this checkout's dist/ and scripts/, so ./build.sh updates new sessions directly.
# Usage: ./install.sh [--mod "<modifier>"] [--opt key=value]... [--no-default-layout | --layouts-only | --uninstall]
#   --mod "<modifier>"   modifier of the jump keys (default "Ctrl Alt")
#   --opt key=value      line in the bar's plugin config (e.g. notify=false); key= drops it. Options already in
#                        agents.kdl are carried over, so a re-run keeps them
#   --no-default-layout  layouts + jump keys, and drop our default_layout "agents" line (start with: zellij --layout agents)
#   --layouts-only       write the layouts but leave config.kdl alone (try it: zellij --layout agents)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cfg_dir="${ZELLIJ_CONFIG_DIR:-$HOME/.config/zellij}"
cfg="$cfg_dir/config.kdl"
layouts="$cfg_dir/layouts"
mkdir -p "$cfg_dir"   # a fresh zellij install may have none yet, and readlink -f needs it
cfg_real="$(readlink -f "$cfg")"   # config.kdl may be a symlink into a dotfiles repo; edit the target

mode= mod="Ctrl Alt" opts=()
while (($#)); do
  case $1 in
    --uninstall | --layouts-only | --no-default-layout) mode=$1 ;;
    --mod) mod=${2:?--mod needs a modifier, e.g. --mod "Ctrl Alt"}; shift ;;
    --opt) [[ ${2:-} =~ ^[a-z_]+= ]] || { echo "--opt needs key=value" >&2; exit 2; }; opts+=("$2"); shift ;;
    *) echo "usage: $0 [--mod \"<modifier>\"] [--opt key=value]... [--no-default-layout | --layouts-only | --uninstall]" >&2; exit 2 ;;
  esac
  shift
done

# with our default_layout line removed, the user's own line that setup.sh commented out when switching comes back
restore_dl='s|^// zellij-agents replaced: ||'

if [[ $mode == --uninstall ]]; then
  rm -f "$layouts/agents.kdl" "$layouts/agents.swap.kdl"
  sed -i -e '/\/\/ zellij-agents$/d' -e "$restore_dl" "$cfg_real"
  echo "uninstalled (new sessions use zellij's default layout again)"
  exit 0
fi

[[ -f "$here/dist/zellij-agents.wasm" ]] || "$here/build.sh"
mkdir -p "$layouts"
# fresh zellij installs run on built-in defaults without a config file; our keybinds block merges with those
[[ -e "$cfg_real" ]] || { mkdir -p "$(dirname "$cfg_real")"; : > "$cfg_real"; }

# extra lines of the bar's plugin block: those in the current agents.kdl (verbatim), then --opt on top
extra=$(python3 -I -c '
import sys, re, json
path, opts, lines = sys.argv[1], sys.argv[2:], {}
try:
    m = re.search(r"plugin location=\"file:[^\"]*zellij-agents\.wasm\" \{\n(.*?)\n\s*\}", open(path).read(), re.S)
    for line in m.group(1).splitlines() if m else []:
        if line.strip() and line.split()[0] not in ("mode", "scanner"):
            lines[line.split()[0]] = line.strip()
except FileNotFoundError:
    pass
for o in opts:
    k, v = o.split("=", 1)
    if k in ("mode", "scanner"):
        sys.exit(f"--opt {k}: set by install.sh itself")
    if not v:
        lines.pop(k, None)
    else:
        lines[k] = k + " " + (v if re.fullmatch(r"true|false|-?[0-9]+", v) else json.dumps(v, ensure_ascii=False))
print("\n".join(" " * 12 + line for line in lines.values()))
' "$layouts/agents.kdl" ${opts[@]+"${opts[@]}"})

bar=$(cat <<EOF
    pane size=2 borderless=true {
        plugin location="file:$here/dist/zellij-agents.wasm" {
            mode "bar"
            scanner "$here/scripts/zam-scan.py"${extra:+
$extra}
        }
    }
EOF
)

# insert the bar right after the tab-bar pane block (first "}" closing the pane that holds tab-bar)
insert_bar() {
  python3 -I -c '
import sys, re
src, bar = sys.stdin.read(), sys.argv[1]
m = re.search(r"plugin location=\"(zellij:)?tab-bar\"\s*\n\s*\}\n", src)
if not m:
    sys.exit("tab-bar pane not found in default layout")
print(src[:m.end()] + bar + "\n" + src[m.end():], end="")
' "$bar"
}

zellij setup --dump-layout default | insert_bar > "$layouts/agents.kdl"
zellij setup --dump-swap-layout default | insert_bar > "$layouts/agents.swap.kdl"

if [[ $mode == --layouts-only ]]; then
  echo "wrote $layouts/agents.kdl; try it with: zellij --layout agents"
  exit 0
fi

if [[ $mode == --no-default-layout ]]; then
  sed -i -e '/^default_layout "agents" \/\/ zellij-agents$/d' -e "$restore_dl" "$cfg_real"
elif ! grep -q '^default_layout ' "$cfg_real"; then
  cp "$cfg_real" "$cfg_real.zam.bak"
  printf '\ndefault_layout "agents" // zellij-agents\n' >> "$cfg_real"
elif ! grep -q '^default_layout "agents"' "$cfg_real"; then
  echo "config.kdl already sets a different default_layout; change it to \"agents\" yourself:" >&2
  grep -n '^default_layout ' "$cfg_real" >&2
  layout_skipped=1
fi
# jump keys: drop previously installed ones (tagged lines, keeps default_layout), then insert the current set
[[ -f "$cfg_real.zam.bak" ]] || cp "$cfg_real" "$cfg_real.zam.bak"
sed -i -E '/^    shared_among .*\/\/ zellij-agents$/d; /zam_jump.*\/\/ zellij-agents$/d; /^    \} \/\/ zellij-agents$/d' "$cfg_real"
keys=$(python3 -I -c '
import sys
mod, t = sys.argv[1], " // zellij-agents"
bind = lambda key, payload: f"        bind \"{mod} {key}\" {{ MessagePlugin {{ name \"zam_jump\"; payload \"{payload}\"; }}; }}" + t
lines = ["    shared_among \"normal\" \"locked\" {" + t]
lines += [bind(n, n) for n in range(1, 10)]
lines += [bind(0, "panel"), bind("left", "left"), bind("right", "right")]
lines.append("    }" + t)
print("\n".join(lines))
' "$mod")
python3 -I -c '
import sys, re
p, keys = sys.argv[1], sys.argv[2]
src = open(p).read()
m = re.search(r"^keybinds[^\n]*\{\n", src, re.M)
if not m:
    src += "\nkeybinds { // zellij-agents\n" + keys + "\n} // zellij-agents\n"
else:
    src = src[:m.end()] + keys + "\n" + src[m.end():]
open(p, "w").write(src)
' "$cfg_real" "$keys"
if [[ $mode == --no-default-layout ]]; then
  echo "installed: jump keys + layouts, no default_layout; start a session with the bar: zellij --layout agents"
elif [[ -n "${layout_skipped:-}" ]]; then
  echo "installed: jump keys + layouts; set default_layout \"agents\" to get the bar in new sessions"
else
  echo "installed: new zellij sessions get the agents bar ($layouts/agents.kdl)"
fi
echo "first run: the bar asks for permissions; click it and press y. Remembered from then on (bars already open in other tabs ask once too)"
