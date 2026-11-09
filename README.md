# zellij-agents

A Zellij plugin that shows every running coding agent (Claude Code today; Codex/Cursor/agy later) across tabs, stacks and floating panes, with its status, context usage and age, and jumps to its pane on click/enter.

One WASM binary, two modes:

- **`bar`**: a 2-row strip under the native tab bar. Each chip shows the agent number, status, repo@branch and context % on the first row and the session title on the second. Click a chip, or press `Ctrl+Alt+<number>`, to jump; `Ctrl+Alt+←/→` goes to the previous/next agent. The chip of the focused agent is reversed; `↗` marks an agent in another Zellij session.
- **`panel`**: a full table (status, name, tab, time in status, ctx %, model, uptime, task title from the pane). `j/k` select, `enter`/click jump, `n` jump to the next agent waiting on you, `q`/`esc` hide. Best as a floating pane toggled by a key.

## How detection works

No setup is needed for the basics. `scripts/zam-scan.py` (run by the plugin every 2s via `run_command`):

1. reads `~/.claude/sessions/<pid>.json` (Claude Code writes `status` = `busy`/`idle`/`shell`, `cwd`, `name`, `startedAt`),
2. drops dead processes (checks `/proc/<pid>/stat` start time against `procStart` to survive pid reuse),
3. reads `ZELLIJ_SESSION_NAME`/`ZELLIJ_PANE_ID` from `/proc/<pid>/environ` (that is the jump target),
4. computes context usage like Claude Code does: tokens of the last request (`input + cache_read + cache_creation` from the transcript) divided by that model's `max_input_tokens` from Claude Code's own model catalog cache (`~/.claude/cache/model-catalog/`), rounded and clamped to 0–100, which is the same formula Claude Code uses for the statusline's `used_percentage` (honours `CLAUDE_CODE_MAX_CONTEXT_TOKENS` + `DISABLE_COMPACT` from the agent's env). No Claude settings or statusline needed. If a model isn't in the catalog, the raw token count is shown instead of a guessed %.

The plugin joins this with `PaneUpdate`/`TabUpdate` to learn the tab and the pane title (Claude puts the task summary there), and jumps with `focus_terminal_pane` (switches tab, unstacks, shows floating). Agents in another Zellij session are listed with `↗ session` and jump via `switch_session_with_focus`.

Optional hooks (`scripts/install-hooks.sh`) add the **NEEDS YOU** state (permission prompt) and the current tool name, which the session file alone can't tell apart from "busy".

## Build

```sh
./build.sh            # -> dist/zellij-agents.wasm
```

Needs rustup + `wasm32-wasip1`. Without a system C compiler it uses zig from `~/.local/share/zam-zig` as the host linker (`sudo apt install build-essential` removes the need).

## Install (bar in every new session)

```sh
./install.sh               # layouts/agents.kdl + agents.swap.kdl in ~/.config/zellij/layouts, default_layout "agents" in config.kdl
./install.sh --uninstall   # undo
```

The layout is generated from zellij's own default layout and swap layouts, with the bar added under the tab bar. It points at this checkout, so `./build.sh` updates new sessions directly. Existing sessions keep their layout; start a new session (or `zellij action new-tab --layout agents`) to get the bar.

## Jump keys

`./install.sh` adds these to `config.kdl` (lines tagged `// zellij-agents`; re-running replaces them, `--uninstall` removes them):

| key | action |
|---|---|
| `Ctrl+Alt+1` … `Ctrl+Alt+9` | jump to agent n of the bar |
| `Ctrl+Alt+←` / `Ctrl+Alt+→` | previous / next agent from the focused one, wrapping around at both ends |

Each key broadcasts a message to all plugins:

```kdl
bind "Ctrl Alt 1" { MessagePlugin { name "zam_jump"; payload "1"; }; }
```

Only the bar in the tab you are looking at acts on it, so one press is one jump. Payloads: `1`–`9`, `left`, `right`, and `next` (next agent waiting on you, then the next idle one; not bound by default). Numbers follow the bar order: agents in this session by tab, top-to-bottom and left-to-right within a tab, then agents in other sessions.

`Ctrl+Alt+<digit>` needs a terminal that speaks the kitty keyboard protocol (WezTerm: `config.enable_kitty_keyboard = true`; kitty, foot, ghostty and recent Alacritty do by default). Legacy terminal encoding can't express Ctrl with a digit.

Zellij reloads `config.kdl` in running sessions by itself. To load a new plugin build into bars that are already running:

```sh
zellij action start-or-reload-plugin -c "mode=bar,scanner=$HOME/dev/zellij-agents-manager/scripts/zam-scan.py" file:$HOME/dev/zellij-agents-manager/dist/zellij-agents.wasm
```

The configuration must match the layout's exactly, or Zellij starts a new instance instead.

## Try it

Bar in every tab (new session with this layout):

```sh
zellij --layout ~/dev/zellij-agents-manager/layouts/agents.kdl
```

Panel as a floating window in the current session:

```sh
zellij plugin --floating --configuration "mode=panel,scanner=$HOME/dev/zellij-agents-manager/scripts/zam-scan.py" -- file:$HOME/dev/zellij-agents-manager/dist/zellij-agents.wasm
```

Zellij asks for permissions on first load (read state, change state, run commands); press `y`.

Keybinding for the panel (add to the `shared_except "locked"` block of `config.kdl`):

```kdl
bind "Alt a" {
    LaunchOrFocusPlugin "file:~/dev/zellij-agents-manager/dist/zellij-agents.wasm" {
        floating true
        move_to_focused_tab true
        mode "panel"
        scanner "/home/ondra/dev/zellij-agents-manager/scripts/zam-scan.py"
    }
}
```

## Config

| key | default | |
|---|---|---|
| `mode` | `bar` | `bar` or `panel` |
| `scanner` | `zam-scan.py` | absolute path recommended |
| `interval` | `2` | seconds between scans |
| `hide_on_jump` | `true` | panel hides itself after jumping |
| `debug_dump` | – | write each rendered frame to this path |
