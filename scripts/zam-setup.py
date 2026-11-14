#!/usr/bin/env python3
"""Interactive setup (run it through ../setup.sh): a status overview of Zellij and every detected agent, from which
any piece can be installed, changed or removed. Every setting is read back from the files on disk (config.kdl,
agents.kdl, the agents' settings files); nothing is stored separately. The changes themselves are made by
install.sh and scripts/install-*.sh, so there is one implementation of each edit.
  --status  print the overview as plain text and exit"""
import curses
import functools
import json
import locale
import os
import re
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(HERE, "install.sh")
SCAN = os.path.join(HERE, "scripts", "zam-scan.py")
TAG = "// zellij-agents"
REPLACED = "// zellij-agents replaced: "  # the user's default_layout line, put back by install.sh with ours removed
OPT_DEFAULTS = {"notify": "true", "interval": "2", "hide_on_jump": "true"}  # README Config table
MODS = ["Ctrl Alt", "Alt", "Ctrl Shift", "Super"]
JUMP_KEYS = [str(n) for n in range(1, 10)] + ["0", "left", "right"]
STR = re.compile(r'"((?:[^"\\]|\\.)*)"')
KEEP = object()


class State:
    pass


def shq(s):  # jq's @sh, as the install scripts quote the scanner path
    return "'" + s.replace("'", "'\\''") + "'"


def env_dir(var, default):
    return os.environ.get(var) or os.path.expanduser(default)


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def cmd_out(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True).stdout
    except OSError:
        return None


# ---- keybinds: a brace-depth scanner over `bind "…"` lines inside the mode blocks of `keybinds`, no KDL library ----

def parse_keybinds(text):
    """(binds, clear_defaults); binds = [(mode, keys, actions)], mode = (block name, [mode names])"""
    binds, clear, depth, inside, mode, bind = [], False, 0, False, None, None
    for line in text.splitlines():
        bare = re.sub(r"//.*", "", STR.sub('""', line))
        if depth == 0 and re.match(r"\s*keybinds\b", bare):
            inside, clear = True, "clear-defaults=true" in bare.replace(" ", "")
        elif inside and depth == 1 and "{" in bare:
            mode = (bare.split()[0].lower(), [s.lower() for s in STR.findall(line.split("{")[0])])
        elif inside and depth == 2 and re.match(r"\s*bind\b", bare):
            head, _, rest = line.partition("{")
            bind = None if line.rstrip().endswith(TAG) else [mode, STR.findall(head), rest]
            if bind:
                binds.append(bind)
        elif bind:
            bind[2] += "\n" + line
        depth += bare.count("{") - bare.count("}")
        if depth <= 2:
            bind = None
        if depth <= 0:
            inside = False
    return [tuple(b) for b in binds], clear


def covers(mode, m):
    kind, names = mode
    if kind == "shared_except":
        return m not in names
    if kind == "shared_among":
        return m in names
    return kind == "shared" or kind == m


def norm(key):
    *mods, k = key.lower().split()
    return frozenset(mods), k


@functools.lru_cache(maxsize=None)
def stock_binds():
    """zellij's default binds; they don't change while setup runs"""
    return parse_keybinds(cmd_out("zellij", "setup", "--dump-config") or "")[0]


def effective_binds(cfg_text):
    """config.kdl's binds first, then zellij's defaults unless the config clears them"""
    binds, clear = parse_keybinds(cfg_text or "")
    if not clear:
        binds += stock_binds()
    return binds


def detect_mod(binds):
    """the modifier the user's mode keys use: the one on SwitchToMode "pane" in normal mode"""
    for mode, keys, actions in binds:
        if covers(mode, "normal") and re.search(r'SwitchToMode\s+"pane"', actions, re.I):
            for k in keys:
                if len(k.split()) > 1:
                    return " ".join(k.split()[:-1])
    return None


def clashes(binds, mod):
    """binds in normal/locked that the jump keys on mod would shadow, as 'key (action)'"""
    ours = {norm(f"{mod} {k}") for k in JUMP_KEYS}
    out = []
    for mode, keys, actions in binds:
        if covers(mode, "normal") or covers(mode, "locked"):
            for k in keys:
                if norm(k) in ours:
                    what = f'{k} ({actions.strip().splitlines()[0].rstrip("}").strip().rstrip(";")})'
                    if what not in out:
                        out.append(what)
    return out


# ---- state, derived from the files ----

def plugin_opts(layout_text):
    """{key: value} of the bar's plugin block in agents.kdl, besides mode and scanner; comments and bare nodes skipped"""
    m = re.search(r'plugin location="file:[^"]*zellij-agents\.wasm" \{\n(.*?)\n\s*\}', layout_text or "", re.S)
    opts = {}
    for line in m.group(1).splitlines() if m else []:
        if not line.strip():
            continue
        k, v = (line.split(None, 1) + [""])[:2]  # KDL whitespace: a tab as well as a space
        v = v.strip()
        if k and v and not k.startswith("/") and k not in ("mode", "scanner"):
            try:
                opts[k] = json.loads(v) if STR.fullmatch(v) else v
            except ValueError:  # a KDL string JSON doesn't read, e.g. "C:\tmp": shown as written
                opts[k] = v
    return opts


def json_commands(o):
    """every "command" string in a settings file (hooks and status lines), at any depth"""
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "command" and isinstance(v, str):
                yield v
            else:
                yield from json_commands(v)
    elif isinstance(o, list):
        for v in o:
            yield from json_commands(v)


def part_state(path, markers, ours):
    """'installed' / 'stale' / 'missing': our command in a settings file, found by the scripts' patterns; stale while
    any of them points elsewhere (another checkout)"""
    try:
        with open(path) as f:
            found = [s for s in json_commands(json.load(f)) if all(m in s for m in markers)]
    except (OSError, ValueError):
        return "missing"
    if not found:
        return "missing"
    return "installed" if all(s == ours or s.startswith(ours + " ") for s in found) else "stale"


def agents():
    home = os.path.expanduser("~")
    claude = env_dir("CLAUDE_CONFIG_DIR", "~/.claude")
    codex = env_dir("CODEX_HOME", "~/.codex")
    cursor = os.path.join(home, ".cursor")
    agy = os.path.join(home, ".gemini", "antigravity-cli")
    scan = lambda flag: f"python3 {shq(SCAN)} --{flag}"
    zam = lambda flag: ("zam-scan.py", "--" + flag)
    return [  # name, binary, config dir, script, [(part, file, markers, our command)]
        ("Claude", "claude", claude, "install-hooks.sh", [
            ("hooks", f"{claude}/settings.json", ("zam-hook.sh",), os.path.join(HERE, "scripts", "zam-hook.sh")),
            ("status line", f"{claude}/settings.json", zam("claude-statusline"), scan("claude-statusline"))]),
        ("Cursor", "cursor-agent", cursor, "install-cursor.sh", [
            ("hooks", f"{cursor}/hooks.json", zam("cursor-hook"), scan("cursor-hook")),
            ("status line", f"{cursor}/cli-config.json", zam("cursor-statusline"), scan("cursor-statusline"))]),
        ("Codex", "codex", codex, "install-codex.sh", [
            ("hooks", f"{codex}/hooks.json", zam("codex-hook"), scan("codex-hook"))]),
        ("agy", "agy", agy, "install-agy.sh", [
            ("status line", f"{agy}/settings.json", zam("agy-statusline"), scan("agy-statusline"))]),
    ]


def read_state():
    st = State()
    out = cmd_out("zellij", "--version")
    st.zellij = out.split()[-1] if out and out.split() else None
    st.jq = shutil.which("jq") is not None
    st.wasm = os.path.isfile(os.path.join(HERE, "dist", "zellij-agents.wasm"))
    cfg_dir = env_dir("ZELLIJ_CONFIG_DIR", "~/.config/zellij")
    st.cfg = os.path.realpath(os.path.join(cfg_dir, "config.kdl"))  # follows a symlink like install.sh
    cfg = read(st.cfg) or ""
    loc = f'plugin location="file:{HERE}/dist/zellij-agents.wasm"'
    st.layout = os.path.join(cfg_dir, "layouts", "agents.kdl")
    texts = [read(os.path.join(cfg_dir, "layouts", f)) for f in ("agents.kdl", "agents.swap.kdl")]
    st.layouts = "missing" if None in texts else "installed" if all(loc in t for t in texts) else "stale"
    st.opts = plugin_opts(texts[0])
    m = re.search(r"^default_layout\s+(.*)$", cfg, re.M)
    st.dl = (STR.search(m.group(1)) or re.search(r"(\S+)", m.group(1))).group(1) if m else None
    st.dl_tagged = bool(m) and m.group(1).rstrip().endswith(TAG)
    m = re.search(r'bind "(.+) 1" \{ MessagePlugin \{ name "zam_jump".*' + TAG, cfg)
    st.keys_mod = m.group(1) if m else None
    st.binds = effective_binds(cfg) if st.zellij else parse_keybinds(cfg)[0]
    st.detected = detect_mod(st.binds)
    st.default_mod = st.detected or MODS[0]
    st.agents = []
    for name, binary, cfg_home, script, parts in agents():
        found = shutil.which(binary) is not None or os.path.isdir(cfg_home)
        states = [(part, part_state(f, markers, ours)) for part, f, markers, ours in parts]
        st.agents.append((name, found, script, states))
    return st


def zellij_cmds(st, mod=KEEP, dl=KEEP, opts=None):
    """the install.sh runs that turn the current state into the wanted one; install.sh carries the plugin options
    already in agents.kdl over, so only a changed one goes along as --opt"""
    fresh = st.layouts == "missing" and not st.keys_mod and not st.dl_tagged
    if mod is KEEP:
        mod = st.default_mod if fresh else st.keys_mod
    if dl is KEEP:  # install.sh writes default_layout only together with the keys, so removing them drops it too
        dl = mod is not None and (fresh or st.dl == "agents")
    if dl and not mod:
        mod = st.default_mod
    optargs = [a for k, v in (opts or {}).items() for a in ("--opt", f"{k}={v}")]
    if mod:
        return [([] if dl else ["--no-default-layout"]) + ["--mod", mod] + optargs]
    return ([["--uninstall"]] if st.keys_mod or st.dl_tagged else []) + [["--layouts-only"] + optargs]


# ---- status rows: (label, text, action or None, dim) ----

def onoff(st, key):
    v = st.opts.get(key)
    return "on (default)" if v is None else "off" if v == "false" else "on"


def rows(st):
    r = []
    ver = st.zellij
    if ver is None:
        r.append(("zellij", "not found: needed for the layouts and jump keys", None, False))
    else:
        old = tuple(int(x) for x in re.findall(r"\d+", ver)[:2]) < (0, 45)
        r.append(("zellij", ver + ("  ⚠ needs 0.45 or newer" if old else ""), None, False))
    r.append(("jq", "ok" if st.jq else "not found: needed by every agent script", None, False))
    r.append(("plugin build", "dist/zellij-agents.wasm" if st.wasm else "missing (install.sh builds it, needs Rust)",
              None, False))
    z = (lambda f: f) if st.zellij else (lambda f: None)
    need = "" if st.zellij else "  (needs zellij)"
    r.append(("layouts", {"installed": "installed", "missing": "not installed",
                          "stale": "stale: from another checkout"}[st.layouts] + need, z(act_layouts), False))
    dl = ("on" if st.dl == "agents" else "off" if st.dl is None else f'other: "{st.dl}"')
    r.append(("default_layout", dl + need, z(act_default_layout), False))
    if st.keys_mod:
        keys = f"installed: {st.keys_mod}"
        c = clashes(st.binds, st.keys_mod)
        keys += f"  ⚠ clashes with {', '.join(c)}" if c else ""
    else:
        keys = f"not installed (default modifier: {st.default_mod})"
    r.append(("jump keys", keys + need, z(act_keys), False))
    interval = st.opts.get("interval")
    if st.zellij and st.layouts == "missing":  # the options live in agents.kdl: no layouts, nothing to change
        z, need = (lambda f: None), "  (install the layouts first)"
    r.append(("notify", onoff(st, "notify") + need, z(lambda s: act_bool_opt(s, "notify")), False))
    r.append(("interval", (f"{interval} s" if interval else "2 s (default)") + need, z(act_interval), False))
    r.append(("hide_on_jump", onoff(st, "hide_on_jump") + need, z(lambda s: act_bool_opt(s, "hide_on_jump")), False))
    for name, found, script, states in st.agents:
        if not found:
            r.append((name, "not found", None, True))
            continue
        if all(s == "missing" for _, s in states):
            text = "not installed"
        else:
            text = ", ".join(f"{p} " + {"installed": "installed", "missing": "not installed",
                                        "stale": "stale: from another checkout"}[s] for p, s in states)
            if name == "Codex":
                text += " (trust them once in Codex's /hooks)"
        act = (lambda s, a=(name, script, states): act_agent(s, *a)) if st.jq else None
        r.append((name, text + ("" if st.jq else "  (needs jq)"), act, False))
    return r


def status_text(st):
    return "\n".join([f"zellij-agents setup: {HERE}"] + [f"  {label:<15} {text}" for label, text, _, _ in rows(st)])


# ---- menu ----

def pick(title, items, sel=0):
    """arrow-key menu (↑/↓/j/k, enter, q/esc back); items = (text, enabled[, dim]); returns the index or None"""
    return curses.wrapper(_pick, title, items, sel)


def _pick(scr, title, items, sel):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    enabled = [i for i, it in enumerate(items) if it[1]]
    sel = sel if sel in enabled else (enabled or [None])[0]
    top = 0
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        put = lambda y, x, s, a=0: scr.addnstr(y, x, s, max(0, w - x - 1), a) if 0 <= y < h else None
        head = title.splitlines()
        for y, t in enumerate(head):
            put(y, 0, t, curses.A_BOLD if y == 0 else 0)
        y0 = len(head) + 1
        n = max(1, h - y0 - 2)
        if sel is not None:
            top = min(max(top, sel - n + 1), sel)
        for y, i in enumerate(range(top, min(len(items), top + n))):
            text, en, *dim = items[i]
            attr = curses.A_REVERSE if i == sel else curses.A_DIM if (dim and dim[0]) else 0
            put(y0 + y, 0, ("> " if i == sel else "  ") + text, attr)
        put(h - 1, 0, "↑/↓ move   enter select   q back", curses.A_DIM)
        scr.refresh()
        k = scr.getch()
        if k in (ord("q"), 27):
            return None
        if sel is None:
            continue
        if k in (curses.KEY_ENTER, 10, 13):
            return sel
        step = -1 if k in (curses.KEY_UP, ord("k")) else 1 if k in (curses.KEY_DOWN, ord("j")) else 0
        if step:
            pos = enabled.index(sel) + step
            sel = enabled[min(max(pos, 0), len(enabled) - 1)]


def run(cmds):
    """backends run outside curses, their output stays on screen until enter; a callable runs as a step in between"""
    for c in cmds:
        if callable(c):
            c()
            continue
        print("$ " + shlex.join(c), flush=True)
        if subprocess.run(c).returncode != 0:
            print("failed")
            break
    input("\npress enter to continue ")
    return True


def same_mod(a, b):
    return bool(a and b) and norm(a + " x") == norm(b + " x")


def cmds_mod(cmds):
    """the modifier install.sh runs pass as --mod, if any"""
    return next((c[c.index("--mod") + 1] for c in cmds if "--mod" in c), None)


def mod_ok(st, m):
    """a modifier newly getting the jump keys: its clashes shown and confirmed first"""
    c = [] if same_mod(m, st.keys_mod) else clashes(st.binds, m)
    return not c or pick(f"⚠ {m} clashes with these binds, which the jump keys would shadow:\n  " + "\n  ".join(c),
                         [("Cancel", True), (f"Use {m} anyway", True)]) == 1


def zellij_change(st, checked=False, **want):
    """checked: the modifier was confirmed by the caller already"""
    cmds = [[INSTALL] + c for c in zellij_cmds(st, **want)]
    mod = cmds_mod(cmds)
    if mod and not checked and not mod_ok(st, mod):
        return False
    layout = read(st.layout)
    if cmds[0][1:] == ["--uninstall"] and len(cmds) > 1 and layout is not None:
        # --uninstall deletes agents.kdl; put it back so the --layouts-only run after it carries the plugin block's
        # lines over verbatim
        def restore():
            with open(st.layout, "w") as f:
                f.write(layout)
        cmds.insert(1, restore)
    return run(cmds)


def cur(yes):
    return "  (current)" if yes else ""


def act_layouts(st):
    if st.layouts == "missing":
        items = [(f'Install (layouts, jump keys on {st.keys_mod or st.default_mod}, default_layout "agents")', True)]
    else:
        own = st.dl == "agents" and not st.dl_tagged
        items = [("Reinstall / update", True), ("Remove (layouts, jump keys and our default_layout line)"
                                                + ('  ⚠ your own default_layout "agents" stays in config.kdl and '
                                                   "would point at no layout: remove it by hand" if own else ""), True)]
    i = pick(f"Layouts: {st.layouts}", items)
    if i == 0:
        return zellij_change(st)
    if i == 1:
        return run([[INSTALL, "--uninstall"]])
    return False


def act_default_layout(st):
    if st.dl not in (None, "agents"):
        i = pick(f'{st.cfg} sets default_layout "{st.dl}"', [('Switch it to "agents"', True), ("Keep it", True)])
        if i != 0 or pick(f'Replace default_layout "{st.dl}" with "agents" in {st.cfg}?\n'
                          f"(your line stays there as a comment and comes back when the agents' default_layout is "
                          f"turned off or removed)", [("No", True), ("Yes", True)]) != 1:
            return False
        mod = cmds_mod(zellij_cmds(st, dl=True))
        if mod and not mod_ok(st, mod):  # before config.kdl is touched
            return False
        if not os.path.exists(st.cfg + ".zam.bak"):
            shutil.copyfile(st.cfg, st.cfg + ".zam.bak")
        src = re.sub(r"^default_layout\s.*$", lambda m: f'{REPLACED}{m.group(0)}\ndefault_layout "agents" {TAG}',
                     read(st.cfg), count=1, flags=re.M)
        with open(st.cfg, "w") as f:
            f.write(src)
        return zellij_change(st, checked=True, dl=True)
    on = st.dl == "agents"
    off = "off: layouts and jump keys only (start with: zellij --layout agents)"
    if on and not st.dl_tagged:
        off = "off: your own default_layout line in config.kdl, remove it by hand"
    i = pick("default_layout", [('on: new sessions get the bar (default_layout "agents")' + cur(on), True),
                                (off + cur(not on), not on or st.dl_tagged, on and not st.dl_tagged)])
    if i is None or (i == 0) == on:
        return False
    return zellij_change(st, dl=i == 0)


def act_keys(st):
    cands = []
    for m in [st.detected] + MODS:
        if m and norm(m + " x") not in [norm(c + " x") for c in cands]:
            cands.append(m)
    same = same_mod
    items = []
    for m in cands:
        c = clashes(st.binds, m)
        items.append((m + ("  (default)" if same(m, st.detected) else "") + cur(same(m, st.keys_mod))
                      + (f"  ⚠ clashes with {', '.join(c)}" if c else ""), True))
    if st.keys_mod:
        items.append(("Remove the jump keys" + (" (default_layout goes too)" if st.dl_tagged else ""), True))
    i = pick("Jump-key modifier: <mod>+1..9 jump, <mod>+0 panel, <mod>+←/→ previous/next", items)
    if i is None:
        return False
    if i == len(cands):
        return zellij_change(st, mod=None)
    m = cands[i]
    if same(m, st.keys_mod):
        return False
    return zellij_change(st, mod=m)  # confirms m's clashes


def set_opt(st, key, value):
    """unset = plugin default, so a value equal to the default drops the line"""
    value = "" if value == OPT_DEFAULTS[key] else value
    if st.opts.get(key, "") == value:
        return False
    return zellij_change(st, opts={key: value})


def act_bool_opt(st, key):
    on = st.opts.get(key) != "false"
    i = pick(f"{key}: {onoff(st, key)}", [("on" + cur(on), True), ("off" + cur(not on), True)])
    return False if i is None else set_opt(st, key, "true" if i == 0 else "false")


def act_interval(st):
    cur_v = st.opts.get("interval", OPT_DEFAULTS["interval"])
    v = input(f"interval in seconds between scans (integer ≥ 1, default 2; enter keeps {cur_v}): ").strip()
    if not v:
        return False
    if not re.fullmatch(r"[0-9]+", v) or int(v) < 1:
        input(f"not an integer ≥ 1: {v!r}; press enter ")
        return False
    return set_opt(st, "interval", str(int(v)))


def act_agent(st, name, script, states):
    installed = any(s != "missing" for _, s in states)
    items = [("Reinstall / update" if installed else "Install", True)] + ([("Remove", True)] if installed else [])
    i = pick(f"{name}: " + ", ".join(f"{p} {s}" for p, s in states), items)
    if i is None:
        return False
    return run([[os.path.join(HERE, "scripts", script)] + (["--uninstall"] if i == 1 else [])])


def main(argv):
    if argv == ["--status"]:
        print(status_text(read_state()))
        return 0
    if argv:
        print("usage: setup.sh [--status]", file=sys.stderr)
        return 2
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    changed, sel = False, 0
    while True:
        st = read_state()
        r = rows(st)
        items = [(f"{label:<15} {text}", act is not None, dim) for label, text, act, dim in r] + [("Quit", True)]
        sel = pick(f"zellij-agents setup: {HERE}", items, sel)
        if sel is None or sel == len(r):
            break
        changed |= bool(r[sel][2](st))
    if changed:
        print("Changed: start a new Zellij session for the layout and restart running agents for their hooks.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
