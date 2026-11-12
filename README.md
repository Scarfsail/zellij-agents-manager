# zellij-agents

A Zellij plugin that shows every running coding agent (Claude Code and Cursor CLI today; Codex/agy later) across tabs, stacks and floating panes, with its status, context usage and age, and jumps to its pane on click/enter.

One WASM binary, two modes:

- **`bar`**: a 2-row strip under the native tab bar. Each chip shows the agent number, status, repo@branch and context % on the first row and the session title on the second. Click a chip, or press `Ctrl+Alt+<number>`, to jump; `Ctrl+Alt+←/→` goes to the previous/next agent. The chip of the focused agent is reversed; `↗` marks an agent in another Zellij session. A working agent shows a spinning `◐`; the chip of an agent waiting on you shows `?` and pulses amber until you focus it; after that only the `?` pulses, until it asks something new (needs the hooks below).
- **`panel`**: a full table (status, name, tab, time in status, ctx %, model, uptime, task title from the pane). `j/k` select, `enter`/click jump, `n` jump to the next agent waiting on you, `q`/`esc` hide. Best as a floating pane: clicking the bar's header block (` AGENTS N `) or `Ctrl+Alt+0` toggles it, or bind a key yourself (below).

## How detection works

No setup is needed for the basics. `scripts/zam-scan.py` (run by the plugin every 2s via `run_command`):

1. reads `~/.claude/sessions/<pid>.json` (Claude Code writes `status` = `busy`/`idle`/`shell`, `cwd`, `name`, `startedAt`),
2. drops dead processes (checks `/proc/<pid>/stat` start time against `procStart` to survive pid reuse),
3. reads `ZELLIJ_PANE_ID` from `/proc/<pid>/environ` and finds the agent's session by walking up to the Zellij server process it runs under (that is the jump target). `ZELLIJ_SESSION_NAME` in the environment is frozen at start and goes stale when a session is renamed; the current name is the server's socket file name, matched to the server pid via `SO_PEERCRED` once a rename is detected,
4. computes context usage like Claude Code does: tokens of the last request (`input + cache_read + cache_creation` from the transcript) divided by that model's `max_input_tokens` from Claude Code's own model catalog cache (`~/.claude/cache/model-catalog/`), rounded and clamped to 0–100, which is the same formula Claude Code uses for the statusline's `used_percentage` (honours `CLAUDE_CODE_MAX_CONTEXT_TOKENS` + `DISABLE_COMPACT` from the agent's env). No Claude settings or statusline needed. If a model isn't in the catalog, the raw token count is shown instead of a guessed %.

The plugin joins this with `PaneUpdate`/`TabUpdate` to learn the tab and the pane title (Claude puts the task summary there), and jumps with `focus_terminal_pane` (switches tab, unstacks, shows floating). Agents in another Zellij session are listed with `↗ session` and jump via `switch_session_with_focus`.

Optional hooks (`scripts/install-hooks.sh`) add the **NEEDS YOU** state (permission prompt) and the current tool name, which the session file alone can't tell apart from "busy".

### Cursor

Cursor CLI (`cursor-agent`) writes no live session file, so the scanner finds its processes in `/proc` (cmdline contains `cursor-agent/versions/…/index.js`) and follows the `~/.cursor/chats/<hash>/<agentId>/store.db` each one keeps open to the chat's `meta.json` (cwd) and the `store.db` meta row (agent id, name, last model). Pane and session come from the environment as for Claude. Without setup, an agent counts as busy while its `store.db` was written in the last 4 s.

`scripts/install-cursor.sh` (idempotent, `.zam.bak` backups, `--uninstall` to undo; restart running agents afterwards) wires the scanner into Cursor:

- **hooks** in `~/.cursor/hooks.json` (`beforeSubmitPrompt`, `preToolUse`, `postToolUse`, `postToolUseFailure`, `stop`, `sessionEnd` → `zam-scan.py --cursor-hook`) give exact busy/idle and the current tool. Existing hooks are kept.
- **status line** in `~/.cursor/cli-config.json` (`zam-scan.py --cursor-statusline`) records Cursor's own context %. If you already have a status line command, it is chained: it gets the same input and its output is still shown. If you had none, the installed one prints nothing and Cursor's status line area stays blank: it only records context and chains to a previous command.

Both write `~/.cache/zellij-agents/cursor/<agent pid>.json`; the hook/status line process finds its agent by walking up its parent processes. Cursor has no permission-prompt hook, so a Cursor agent never shows **NEEDS YOU**.

## Build

```sh
./build.sh            # -> dist/zellij-agents.wasm
```

Needs rustup + `wasm32-wasip1`. Without a system C compiler it uses zig from `~/.local/share/zam-zig` as the host linker (`sudo apt install build-essential` removes the need).

## Install (bar in every new session)

```sh
./install.sh                # layouts/agents.kdl + agents.swap.kdl in ~/.config/zellij/layouts, default_layout "agents" in config.kdl
./install.sh --layouts-only # only the layouts; config.kdl untouched
./install.sh --uninstall    # undo
```

On another machine: clone the repo anywhere, run `./install.sh`, start a new session. Needs Zellij 0.45 and `python3`; the built `dist/zellij-agents.wasm` is committed, so Rust is only needed to change the plugin. If there is no `config.kdl` yet, one is created holding only the jump keys (Zellij's defaults stay in effect). If `config.kdl` already sets another `default_layout`, the script installs the keys and layouts and asks you to switch the layout yourself.

On first start the bar asks for permissions (read state, change state, run commands, message and launch other plugins; the last one opens the panel from the header): click it and press `y`. Zellij remembers this per plugin path; bars that were already open in other tabs ask once too.

The layout is generated from zellij's own default layout and swap layouts, with the bar added under the tab bar. It points at this checkout, so `./build.sh` updates new sessions directly. Existing sessions keep their layout; start a new session (or `zellij action new-tab --layout agents`) to get the bar.

## Jump keys

`./install.sh` adds these to `config.kdl` (lines tagged `// zellij-agents`; re-running replaces them, `--uninstall` removes them):

| key | action |
|---|---|
| `Ctrl+Alt+1` … `Ctrl+Alt+9` | jump to agent n of the bar |
| `Ctrl+Alt+0` | toggle the agents panel (same as clicking the bar's header) |
| `Ctrl+Alt+←` / `Ctrl+Alt+→` | previous / next agent from the focused one, wrapping around at both ends |

Each key broadcasts a message to all plugins:

```kdl
bind "Ctrl Alt 1" { MessagePlugin { name "zam_jump"; payload "1"; }; }
```

Only the bar in the tab you are looking at acts on it, so one press is one jump. Payloads: `1`–`9`, `left`, `right`, `panel` (toggle the agents panel), and `next` (next agent waiting on you, then the next idle one; no default key, bind it yourself if you want one). Numbers follow the bar order: agents in this session by tab, top-to-bottom and left-to-right within a tab, then agents in other sessions.

`Ctrl+Alt+<digit>` needs a terminal that speaks the kitty keyboard protocol (WezTerm: `config.enable_kitty_keyboard = true`; kitty, foot, ghostty and recent Alacritty do by default). Legacy terminal encoding can't express Ctrl with a digit.

Zellij reloads `config.kdl` in running sessions by itself. To load a new plugin build into bars that are already running:

```sh
# run from the checkout; must match the bar config install.sh wrote into the layout
zellij action start-or-reload-plugin -c "mode=bar,scanner=$PWD/scripts/zam-scan.py" "file:$PWD/dist/zellij-agents.wasm"
```

The configuration must match the layout's exactly, or Zellij starts a new instance instead.

## Try it

Bar in every tab (new session with this layout):

```sh
./install.sh --layouts-only   # writes ~/.config/zellij/layouts/agents.kdl pointing at this checkout
zellij --layout agents
```

Panel as a floating window in the current session:

```sh
# run from the checkout
zellij plugin --floating --configuration "mode=panel,scanner=$PWD/scripts/zam-scan.py" -- "file:$PWD/dist/zellij-agents.wasm"
```

Zellij asks for permissions on first load (read state, change state, run commands, message and launch other plugins); press `y`.

Clicking the bar's header (or `Ctrl+Alt+0`) toggles the panel without any setup: it opens a floating panel with the bar's own config and `mode=panel`. A keybinding of your own for the panel (add to the `shared_except "locked"` block of `config.kdl`):

```kdl
bind "Alt a" {
    LaunchOrFocusPlugin "file:~/dev/zellij-agents-manager/dist/zellij-agents.wasm" {   // your checkout path; ~ works
        floating true
        move_to_focused_tab true
        mode "panel"
        scanner "~/dev/zellij-agents-manager/scripts/zam-scan.py"
    }
}
```

The keybinding and the header click reach the same panel only when the plugin URL and the config (`scanner`, `interval`, …) match the bar layout's exactly, as absolute paths the way `install.sh` writes them (no `~`), plus `mode "panel"`; otherwise there are two panel instances.

## Config

| key | default | |
|---|---|---|
| `mode` | `bar` | `bar` or `panel` |
| `scanner` | `zam-scan.py` | absolute path, or `~/…` |
| `interval` | `2` | seconds between scans |
| `hide_on_jump` | `true` | panel hides itself after jumping |
| `debug_dump` | – | write each rendered frame to this path |
