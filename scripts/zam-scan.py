#!/usr/bin/env python3
"""Discover running coding agents and the zellij pane each one lives in.

Prints a JSON array on stdout; the zellij plugin runs this on a timer.

Claude Code: every interactive process writes ~/.claude/sessions/<pid>.json
(status busy/idle/shell, cwd, name, startedAt). We keep the live ones, read
ZELLIJ_SESSION_NAME / ZELLIJ_PANE_ID from /proc/<pid>/environ, take context
usage from the tail of the transcript (window size from Claude's model catalog cache), and overlay the last hook event
(written by zam-hook.sh) to detect "waiting for permission".

Cursor CLI: found by its cmdline plus the ~/.cursor/chats/<hash>/<agentId>/store.db it keeps open.
Busy/idle and the current tool come from Cursor hooks (--cursor-hook), context usage from its
status line (--cursor-statusline); both write ~/.cache/zellij-agents/cursor/<agent pid>.json.
Without hooks, a recently written store.db means busy.

Codex CLI: found by its native `codex` binary in /proc, linked to the newest `codex-tui` thread in
~/.codex/state_5.sqlite with its cwd; busy/idle, context and the account's 5h/week limits come from that
thread's rollout log. Codex hooks (--codex-hook) add "waiting for approval" and the current tool,
in ~/.cache/zellij-agents/codex/<thread id>.json.

Account usage (--fetch-usage): Claude's /usage numbers from api.anthropic.com/api/oauth/usage with the
local login token, cached in ~/.cache/zellij-agents/usage/claude.json and refreshed in the background.
"""
import fcntl
import glob
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR", os.path.join(HOME, ".claude"))
HOOK_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "zellij-agents", "claude")
# "read" marks for questions: <session_id> holds the status_since of the question the user has looked at
SEEN_DIR = os.path.join(os.path.dirname(HOOK_DIR), "seen")
# last status seen per agent id, plus <id>@<status>@<since> claims so only one bar notifies per transition
NOTIFY_DIR = os.path.join(os.path.dirname(HOOK_DIR), "notify")
CURSOR_DIR = os.path.join(os.path.dirname(HOOK_DIR), "cursor")
CURSOR_CHATS = os.path.join(HOME, ".cursor", "chats")
CODEX_DIR = os.path.join(os.path.dirname(HOOK_DIR), "codex")
CODEX_HOME = os.environ.get("CODEX_HOME", os.path.join(HOME, ".codex"))
USAGE_DIR = os.path.join(os.path.dirname(HOOK_DIR), "usage")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# refetch account usage when the cache is older than this; a running fetch blocks another for FETCH_WAIT_S
USAGE_MAX_AGE_S = 60
FETCH_WAIT_S = 30
TAIL_BYTES = 256 * 1024
# Without hooks, a Cursor agent counts as busy while its chat db was written within this many seconds
CURSOR_BUSY_WINDOW_S = 4
# Windows toast for WSL; skipped while the terminal (ZAM_TERM) is the foreground app.
# Text comes in via env and goes through CreateTextNode, so it is never parsed as script or XML.
# One statement per line: -Command - reads stdin line by line.
TOAST_PS = r"""
Add-Type -Name W -Namespace U -MemberDefinition '[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow(); [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(System.IntPtr h, out uint p);'
$fp = 0; [void][U.W]::GetWindowThreadProcessId([U.W]::GetForegroundWindow(), [ref]$fp)
if ($env:ZAM_TERM -and (Get-Process -Id $fp -ErrorAction SilentlyContinue).ProcessName -eq $env:ZAM_TERM) { exit }
$m = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$x = $m::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $x.GetElementsByTagName('text'); [void]$t.Item(0).AppendChild($x.CreateTextNode($env:ZAM_TITLE)); [void]$t.Item(1).AppendChild($x.CreateTextNode($env:ZAM_BODY))
$m::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def seen_ts(sid):
    try:
        with open(os.path.join(SEEN_DIR, sid)) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def safe_id(sid):
    """usable as a file name in our cache dirs"""
    return bool(sid) and "/" not in sid and not sid.startswith(".")


def mark_seen(sid, ts):
    if not safe_id(sid):
        return
    os.makedirs(SEEN_DIR, exist_ok=True)
    tmp = os.path.join(SEEN_DIR, f".{sid}.tmp")
    with open(tmp, "w") as f:
        f.write(str(int(ts)))
    os.replace(tmp, os.path.join(SEEN_DIR, sid))


def write_json(path, obj):
    """atomically replace path with obj as JSON"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def proc_env(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    env = {}
    for item in raw.split(b"\0"):
        k, _, v = item.partition(b"=")
        if k in (b"ZELLIJ_SESSION_NAME", b"ZELLIJ_PANE_ID", b"DISABLE_COMPACT", b"CLAUDE_CODE_MAX_CONTEXT_TOKENS"):
            env[k.decode()] = v.decode(errors="replace")
    return env


def proc_start(pid):
    # field 22 of /proc/<pid>/stat; guards against pid reuse
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        return data[data.rindex(")") + 2 :].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def proc_ppid(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        return int(data[data.rindex(")") + 2 :].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def proc_started_ms(pid):
    """process start in epoch ms: boot time + start ticks"""
    try:
        with open("/proc/stat") as f:
            btime = next(int(l.split()[1]) for l in f if l.startswith("btime "))
        return int((btime + int(proc_start(pid)) / os.sysconf("SC_CLK_TCK")) * 1000)
    except (OSError, ValueError, TypeError, StopIteration):
        return 0


def _socket_dirs():
    bases = []
    if os.environ.get("ZELLIJ_SOCKET_DIR"):
        bases.append(os.environ["ZELLIJ_SOCKET_DIR"])
    if os.environ.get("XDG_RUNTIME_DIR"):
        bases.append(os.path.join(os.environ["XDG_RUNTIME_DIR"], "zellij"))
    bases.append(f"/run/user/{os.getuid()}/zellij")
    bases.append(f"/tmp/zellij-{os.getuid()}")
    # sockets live in a per-protocol subdir (e.g. contract_version_1), or directly in the base
    return [p for b in dict.fromkeys(bases) for p in [b, *glob.glob(os.path.join(b, "*"))] if os.path.isdir(p)]


def _is_socket(path):
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _server_pid(path):
    """pid listening on a zellij socket (SO_PEERCRED); connect + close, nothing is sent"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(0.3)
        s.connect(path)
        return struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
    except OSError:
        return None
    finally:
        s.close()


_SERVERS = None


def zellij_servers():
    """server pid -> current session name.

    A session's name lives in its socket file name; renaming a session renames the socket, but the
    server's cmdline, the kernel's socket table and every pane's ZELLIJ_SESSION_NAME keep the old
    name. While every server's original socket still exists and no two servers started under the
    same name, cmdline names are current; otherwise ask each socket which pid serves it."""
    global _SERVERS
    if _SERVERS is not None:
        return _SERVERS
    _SERVERS = {}
    renamed = False
    for d in glob.glob("/proc/[0-9]*"):
        try:
            with open(f"{d}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if len(argv) >= 3 and argv[0].endswith(b"zellij") and argv[1] == b"--server":
            path = argv[2].decode(errors="replace")
            _SERVERS[int(d[6:])] = os.path.basename(path)
            renamed |= not _is_socket(path)
    names = list(_SERVERS.values())
    renamed |= len(names) != len(set(names))  # a new session took the old name of a renamed one
    if renamed:
        for sd in _socket_dirs():
            for name in os.listdir(sd):
                path = os.path.join(sd, name)
                if _is_socket(path):
                    pid = _server_pid(path)
                    if pid in _SERVERS:
                        _SERVERS[pid] = name
    return _SERVERS


def zellij_session_of(pid, fallback):
    """current session name of the zellij server this process runs under"""
    servers = zellij_servers()
    for _ in range(64):
        if pid is None or pid <= 1:
            break
        if pid in servers:
            return servers[pid]
        pid = proc_ppid(pid)
    return fallback


def pane_fields(pid, env):
    return {
        # ZELLIJ_SESSION_NAME is frozen at process start; a renamed session would jump to the old name
        "zellij_session": zellij_session_of(pid, env.get("ZELLIJ_SESSION_NAME")) if env.get("ZELLIJ_SESSION_NAME") else None,
        "pane_id": int(env["ZELLIJ_PANE_ID"]) if env.get("ZELLIJ_PANE_ID", "").isdigit() else None,
    }


def transcript_path(cwd, session_id):
    enc = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    p = os.path.join(CLAUDE_DIR, "projects", enc, f"{session_id}.jsonl")
    if os.path.exists(p):
        return p
    hits = glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None


def _scan_lines(lines, info):
    """Fill info from transcript lines (newest first): usage/model, titles, git branch."""
    for line in lines:
        if "ctx_tokens" not in info and b'"usage"' in line:
            try:
                d = json.loads(line)
            except ValueError:
                d = None
            m = d.get("message") if isinstance(d, dict) else None
            if d and d.get("type") == "assistant" and isinstance(m, dict) and m.get("usage"):
                u = m["usage"]
                info["ctx_tokens"] = (
                    u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                )
                info["model"] = m.get("model")
        for key, typ, field in (("custom_title", b'"custom-title"', "customTitle"), ("ai_title", b'"ai-title"', "aiTitle")):
            if key not in info and typ in line:
                try:
                    v = json.loads(line).get(field)
                except (ValueError, AttributeError):
                    v = None
                if v:
                    info[key] = v
        if "branch" not in info and b'"gitBranch"' in line:
            try:
                br = json.loads(line).get("gitBranch")
            except (ValueError, AttributeError):
                br = None
            if br:
                info["branch"] = br


def tail_scan(path, scan, done):
    """info filled by scan(lines newest first, info) from the last TAIL_BYTES of a JSONL log;
    if that is not done(info), from the whole file once (a line written long ago and not repeated since)"""
    info = {}
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            scan(reversed(f.read().splitlines()), info)
            if size > TAIL_BYTES and not done(info):
                f.seek(0)
                scan(reversed(f.read().splitlines()), info)
    except OSError:
        pass
    return info


def transcript_info(path):
    """Context tokens + model (last assistant usage), session title, git branch."""
    if not path:
        return {}
    info = tail_scan(path, _scan_lines, lambda i: {"custom_title", "ai_title"} & i.keys())
    # Claude: custom-title (/rename) beats ai-title; both are last-wins
    info["title"] = info.pop("custom_title", None) or info.pop("ai_title", None) or ""
    info.pop("ai_title", None)
    return info


def hook_state(session_id, suffix=""):
    """last hook event (<sid>.json), or with suffix ".status" the status line's record (<sid>.status.json)"""
    try:
        with open(os.path.join(HOOK_DIR, f"{session_id}{suffix}.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


_WINDOWS = None


def context_windows():
    """model id -> max input tokens, from Claude Code's own model catalog cache
    (~/.claude/cache/model-catalog/*.json, refreshed by Claude Code itself)."""
    global _WINDOWS
    if _WINDOWS is not None:
        return _WINDOWS
    _WINDOWS = {}
    files = glob.glob(os.path.join(CLAUDE_DIR, "cache", "model-catalog", "*.json"))
    for path in sorted(files, key=os.path.getmtime):  # newest wins
        try:
            with open(path) as f:
                cat = json.load(f).get("catalog") or {}
            # v2 nests the list under catalog.config.models; v1 had catalog.models
            models = (cat.get("config") or {}).get("models") or cat.get("models") or []
        except (OSError, ValueError, AttributeError):
            continue
        for m in models:
            w = (m.get("runtime") or {}).get("max_input_tokens")
            if m.get("id") and w:
                _WINDOWS[m["id"]] = w
    return _WINDOWS


def context_window(model):
    if not model:
        return None
    w = context_windows()
    if model in w:
        return w[model]
    # dated ids (claude-haiku-4-5-20251001) vs catalog ids, either direction
    base = re.sub(r"-\d{8}$", "", model)
    for mid, size in w.items():
        if re.sub(r"-\d{8}$", "", mid) == base:
            return size
    return None


def claude_agents():
    out = []
    for path in glob.glob(os.path.join(CLAUDE_DIR, "sessions", "*.json")):
        try:
            with open(path) as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        pid = s.get("pid")
        if not pid or s.get("kind", "interactive") != "interactive":
            continue
        if s.get("procStart") and proc_start(pid) != str(s["procStart"]):
            continue  # dead, or pid reused
        env = proc_env(pid)
        if env is None:
            continue
        status = s.get("status", "unknown")
        status_ts = s.get("statusUpdatedAt") or s.get("updatedAt") or 0
        detail = ""
        hs = hook_state(s.get("sessionId", ""))
        if hs:
            ev = hs.get("event")
            asks = ev == "PermissionRequest" or (ev == "Notification" and hs.get("notification_type") == "permission_prompt")
            # stale if Claude's own status changed after the prompt (e.g. user hit Esc)
            if asks and hs.get("ts", 0) >= status_ts:
                status, detail = "needs_input", hs.get("message") or hs.get("tool") or "permission"
                status_ts = max(status_ts, hs.get("ts", 0))
            elif ev in ("PreToolUse", "PostToolUse") and status == "busy":
                detail = hs.get("tool") or ""
        a = {
            "kind": "claude",
            "pid": pid,
            "id": s.get("sessionId"),
            "name": s.get("name") or os.path.basename(s.get("cwd", "")),
            "cwd": s.get("cwd"),
            "started_at": s.get("startedAt", 0),
            "status": status,
            "status_since": status_ts,
            # the user has focused this agent since the current question arrived
            "seen": status == "needs_input" and seen_ts(s.get("sessionId", "")) >= status_ts,
            "detail": detail,
            **pane_fields(pid, env),
            "version": s.get("version"),
            "cost_usd": (hook_state(s.get("sessionId", ""), ".status") or {}).get("cost_usd"),
        }
        a.update(transcript_info(transcript_path(s.get("cwd", ""), s.get("sessionId", ""))))
        win = context_window(a.get("model"))
        # Claude Code (Xh): CLAUDE_CODE_MAX_CONTEXT_TOKENS overrides the window when DISABLE_COMPACT is set
        if env.get("DISABLE_COMPACT", "").lower() in ("1", "true", "yes", "on") and env.get(
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS", ""
        ).isdigit() and int(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"]) > 0:
            win = int(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"])
        if win and a.get("ctx_tokens") is not None:
            a["ctx_size"] = win
            # same as Claude Code's statusline used_percentage (MIr): round, clamp 0..100
            a["ctx_pct"] = min(100, max(0, round(a["ctx_tokens"] / win * 100)))
        out.append(a)
    return out


def is_cursor(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
    except OSError:
        return False
    return any(b"cursor-agent/versions/" in a and a.endswith(b"index.js") for a in argv)


def cursor_pid():
    """the Cursor agent this hook / status line command runs under"""
    pid = os.getppid()
    for _ in range(64):
        if pid is None or pid <= 1:
            return None
        if is_cursor(pid):
            return pid
        pid = proc_ppid(pid)
    return None


def cursor_state(pid):
    try:
        with open(os.path.join(CURSOR_DIR, f"{pid}.json")) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {}
    return st if isinstance(st, dict) and st.get("proc_start") == proc_start(pid) else {}  # else pid reused


def cursor_update(fields):
    """merge fields into the state file of our Cursor agent; None deletes it"""
    pid = cursor_pid()
    if not pid:
        return
    os.makedirs(CURSOR_DIR, exist_ok=True)
    path = os.path.join(CURSOR_DIR, f"{pid}.json")
    lock = os.open(CURSOR_DIR, os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)  # hooks and the status line run concurrently
        if fields is None:
            if os.path.exists(path):
                os.remove(path)
            return
        st = cursor_state(pid)
        # since: when busy/idle last flipped, so a long busy turn keeps its age across tool calls
        if "event" in fields and ("since" not in st or (fields["event"] in CURSOR_BUSY) != (st.get("event") in CURSOR_BUSY)):
            fields["since"] = fields["ts"]
        st.update(fields, proc_start=proc_start(pid))
        tmp = os.path.join(CURSOR_DIR, f".{pid}.tmp")
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, path)
    finally:
        os.close(lock)


def cursor_hook():
    # never block Cursor or deny a tool: no stdout, no errors
    try:
        h = json.load(sys.stdin)
        ev = h.get("hook_event_name")
        if ev == "sessionEnd":
            cursor_update(None)
        else:
            cursor_update({"event": ev, "tool": h.get("tool_name") or "", "ts": int(time.time() * 1000)})
    except BaseException:
        pass


def cursor_statusline(chain):
    data = sys.stdin.buffer.read()
    try:
        p = json.loads(data)
        cw = p.get("context_window") or {}
        st = {
            "ctx_pct": cw.get("used_percentage"),
            "ctx_size": cw.get("context_window_size"),
            "ctx_tokens": cw.get("total_input_tokens"),
        }  # None too: a null after /new clears the old value
        model = (p.get("model") or {}).get("id")
        if model:
            st["model"] = model
        st["sl_ts"] = int(time.time() * 1000)
        cursor_update(st)
    except Exception:
        pass
    run_chain(chain, data)


def run_chain(chain, data):
    if chain:
        # the user's own status line: same stdin, its stdout is ours
        sys.exit(subprocess.run(["sh", "-c", chain], input=data).returncode)


def claude_statusline(chain):
    """Claude Code status line wrapper: record the session's cost, then show the user's own status line"""
    data = sys.stdin.buffer.read()
    try:
        p = json.loads(data)
        sid = p.get("session_id") or ""
        if safe_id(sid):
            cost = (p.get("cost") or {}).get("total_cost_usd")
            write_json(os.path.join(HOOK_DIR, f"{sid}.status.json"), {"cost_usd": cost, "ts": int(time.time() * 1000)})
    except Exception:
        pass
    run_chain(chain, data)


CURSOR_BUSY = ("beforeSubmitPrompt", "preToolUse", "postToolUse", "postToolUseFailure")


def cursor_prune():
    """drop state files of agents killed before their sessionEnd hook ran"""
    try:
        lock = os.open(CURSOR_DIR, os.O_RDONLY)
    except OSError:
        return
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for path in glob.glob(os.path.join(CURSOR_DIR, "*.json")):
            pid = os.path.basename(path)[:-5]
            if not pid.isdigit():
                continue
            try:
                with open(path) as f:
                    st = json.load(f)
                alive = proc_start(int(pid)) is not None and st.get("proc_start") == proc_start(int(pid))
            except (OSError, ValueError, AttributeError):
                alive = False
            if not alive:
                try:
                    os.remove(path)
                except OSError:
                    pass
    finally:
        os.close(lock)


def cursor_agents():
    cursor_prune()
    chats = os.path.realpath(CURSOR_CHATS)  # fd targets are resolved paths
    out = []
    for d in glob.glob("/proc/[0-9]*"):
        pid = int(d[6:])
        try:
            if not is_cursor(pid):
                continue
            db = None
            for fd in os.listdir(f"{d}/fd"):
                try:
                    t = os.readlink(f"{d}/fd/{fd}")
                except OSError:
                    continue
                if t.startswith(chats + "/") and t.endswith("/store.db"):
                    db = t
                    break
            env = proc_env(pid)
            if not db or not env or not env.get("ZELLIJ_PANE_ID"):
                continue
            chat = os.path.dirname(db)
            with open(os.path.join(chat, "meta.json")) as f:
                meta = json.load(f)
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.2)
            try:
                row = con.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
            finally:
                con.close()
            v = row[0] if row else None
            m = json.loads(bytes.fromhex(v.decode() if isinstance(v, bytes) else v)) if v else {}
            m.pop("blobEncryptionKey", None)  # credential: never let it reach the output
            cwd = meta.get("cwd") or ""
            st = cursor_state(pid)
            detail = ""
            hooked = st.get("event") == "stop" or st.get("event") in CURSOR_BUSY
            if st.get("event") == "stop":
                status, status_ts = "idle", st.get("since", st.get("ts", 0))
            elif st.get("event") in CURSOR_BUSY:
                status, status_ts = "busy", st.get("since", st.get("ts", 0))
                detail = st.get("tool", "") if st["event"] != "beforeSubmitPrompt" else ""
            else:
                mt = max((os.path.getmtime(p) for p in (db, db + "-wal") if os.path.exists(p)), default=0)
                status, status_ts = ("busy" if time.time() - mt < CURSOR_BUSY_WINDOW_S else "idle"), int(mt * 1000)
            name = m.get("name") or ""
            a = {
                "kind": "cursor",
                "pid": pid,
                "id": m.get("agentId") or os.path.basename(chat),
                "name": os.path.basename(cwd),
                "cwd": cwd,
                "started_at": proc_started_ms(pid),
                "status": status,
                "status_since": status_ts,
                "seen": False,
                "detail": detail,
                "hooked": hooked,  # status from hooks, not guessed from db writes: only then notify
                **pane_fields(pid, env),
                "model": st.get("model") or m.get("lastUsedModel"),
                # empty: the plugin falls back to the pane title
                "title": "" if name == "New Agent" else name,
            }
            a.update({k: st[k] for k in ("ctx_pct", "ctx_size", "ctx_tokens") if k in st})
            out.append(a)
        except Exception:
            continue  # a malformed chat dir must not break discovery
    return out


# argv[1] of codex runs that are not an interactive TUI (codex-cli 0.167); app-server is the shared daemon
CODEX_NON_TUI = {
    b"exec", b"e", b"review", b"login", b"logout", b"mcp", b"mcp-server", b"plugin", b"app-server",
    b"remote-control", b"completion", b"update", b"doctor", b"sandbox", b"debug", b"apply", b"a", b"queue",
    b"archive", b"delete", b"migrate-rollouts", b"migrate-nfs-db", b"unarchive", b"cloud", b"exec-server",
    b"features", b"help",
}
CODEX_EVENTS = (b'"task_started"', b'"task_complete"', b'"turn_aborted"', b'"token_count"')
# Codex's context % leaves out this many tokens of fixed prompt overhead, on both sides
CODEX_BASELINE_TOKENS = 12000


def is_codex(pid):
    """the native codex binary (not its node wrapper) running the interactive TUI"""
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
        if os.path.basename(exe.removesuffix(" (deleted)")) != "codex":  # " (deleted)": upgraded on disk
            return False
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
    except OSError:
        return False
    # argv[0] is codex for the TUI (the npm wrapper passes the binary's path); helpers re-exec it under their own name
    return os.path.basename(argv[0]) == b"codex" and (len(argv) < 2 or argv[1] not in CODEX_NON_TUI)


def codex_query(sql, *args):
    con = sqlite3.connect(f"file:{os.path.join(CODEX_HOME, 'state_5.sqlite')}?mode=ro", uri=True, timeout=0.2)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _scan_rollout(lines, info):
    """Fill info from Codex rollout lines (newest first): the last turn start/end, context, account rate limits."""
    for line in lines:
        if "last_ts" not in info:  # the newest line: whether the turn moved on after a hook
            m = re.match(rb'\{"timestamp":"([^"]+)"', line)
            try:
                info["last_ts"] = _epoch_ms(m[1].decode()) if m else 0
            except ValueError:
                info["last_ts"] = 0
        if {"status", "ctx_tokens", "rate_limits"} <= info.keys():
            break
        if b'"event_msg"' not in line or not any(t in line for t in CODEX_EVENTS):
            continue
        try:
            d = json.loads(line)
            p, typ, ts = d["payload"], d["payload"]["type"], _epoch_ms(d["timestamp"])
        except (ValueError, KeyError, TypeError):
            continue
        if typ in ("task_started", "task_complete", "turn_aborted") and "status" not in info:
            # turn_aborted: the user pressed Esc, no task_complete follows
            info["status"] = "busy" if typ == "task_started" else "idle"
            info["status_since"] = ts
        elif typ == "token_count":
            usage = (p.get("info") or {}).get("last_token_usage")
            if usage and "ctx_tokens" not in info:
                info["ctx_tokens"], info["ctx_size"] = usage.get("total_tokens"), p["info"].get("model_context_window")
            rl = p.get("rate_limits") or {}
            if (rl.get("primary") or rl.get("secondary")) and "rate_limits" not in info:
                info["rate_limits"] = rl


def rollout_info(path, thread_id=None):
    """from the rollout's tail only; with thread_id, a last turn event older than the tail (a turn that logged
    more than TAIL_BYTES) is read from the whole file once and kept in CODEX_DIR/<thread id>.turn.json"""
    info = tail_scan(path, _scan_rollout, lambda i: True) if path else {}
    if not (path and thread_id) or "status" in info or os.path.getsize(path) <= TAIL_BYTES:
        return info
    cache = os.path.join(CODEX_DIR, f"{thread_id}.turn.json")
    try:
        with open(cache) as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = None
    if not (isinstance(c, dict) and c.get("rollout") == path):
        full = {}
        with open(path, "rb") as f:
            _scan_rollout(reversed(f.read().splitlines()), full)
        if "status" not in full:
            return info
        c = {"rollout": path, "status": full["status"], "status_since": full["status_since"]}
        write_json(cache, c)
    info.update(status=c.get("status"), status_since=c.get("status_since"))
    return info


def codex_state(thread_id):
    try:
        with open(os.path.join(CODEX_DIR, f"{thread_id}.json")) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {}
    return st if isinstance(st, dict) else {}


def codex_hook():
    # never block Codex: no stdout, no errors. Hooks run in Codex's shared app-server daemon, so the
    # state is keyed by the thread (session_id), not by a parent pid
    try:
        h = json.load(sys.stdin)
        sid = h.get("session_id") or ""
        if not safe_id(sid):
            return
        path = os.path.join(CODEX_DIR, f"{sid}.json")
        if h.get("hook_event_name") == "SessionEnd":
            os.remove(path)
        else:
            write_json(path, {"event": h.get("hook_event_name"), "tool": h.get("tool_name") or "", "ts": int(time.time() * 1000)})
    except BaseException:
        pass


def codex_prune(live):
    """drop hook state of threads no running TUI links to, after a day (killed before SessionEnd, or not a TUI)"""
    for path in glob.glob(os.path.join(CODEX_DIR, "*.json")):
        age = _age_s(path)
        if os.path.basename(path).split(".")[0] not in live and age is not None and age > 86400:
            try:
                os.remove(path)
            except OSError:
                pass


def codex_agents():
    out = []
    for d in glob.glob("/proc/[0-9]*"):
        pid = int(d[6:])
        try:
            if not is_codex(pid):
                continue
            env = proc_env(pid)
            if not env or not env.get("ZELLIJ_PANE_ID"):
                continue
            started = proc_started_ms(pid)
            cwd = os.readlink(f"{d}/cwd")
            try:
                # the TUI names no thread anywhere: take the newest one it can own (not a subagent's or codex exec's)
                rows = codex_query(
                    "SELECT id, cwd, title, git_branch, model, rollout_path FROM threads WHERE cwd = ? AND archived = 0"
                    " AND thread_source = 'user' AND originator = 'codex-tui' AND updated_at_ms >= ?"
                    " ORDER BY updated_at_ms DESC LIMIT 1",
                    cwd,
                    started,
                )
                tid, cwd, title, branch, model, rollout = rows[0] if rows else (None, cwd, "", None, None, None)
                info, st = rollout_info(rollout, tid), codex_state(tid) if tid else {}
            except Exception:  # unreadable or changed state db: list it unlinked, like a TUI with no thread yet
                tid, title, branch, model, info, st = None, "", None, None, {}, {}
            status, status_ts = info.get("status", "idle"), info.get("status_since", started)
            detail, hook_ts = "", st.get("ts", 0)
            # a rollout line after the request: approved (or denied), the turn goes on
            if st.get("event") == "PermissionRequest" and hook_ts > status_ts and info.get("last_ts", 0) <= hook_ts:
                status, status_ts, detail = "needs_input", hook_ts, st.get("tool") or "permission"
            elif status == "busy" and hook_ts > status_ts:
                if st.get("event") == "PreToolUse":
                    detail = st.get("tool") or ""
                status_ts = hook_ts  # busy again after a request, so the next one in this turn is new
            a = {
                "kind": "codex",
                "pid": pid,
                "id": tid or f"codex-{pid}",
                "name": os.path.basename(cwd),
                "cwd": cwd,
                "started_at": started,
                "status": status,
                "status_since": status_ts,
                "seen": status == "needs_input" and seen_ts(tid) >= status_ts,
                "detail": detail,
                **pane_fields(pid, env),
                "title": title or "",  # empty: the plugin falls back to the pane title
                "branch": branch,
                "model": model,
            }
            tokens, win = info.get("ctx_tokens"), info.get("ctx_size")
            if tokens is not None:
                a["ctx_tokens"] = tokens
            if tokens is not None and win and win > CODEX_BASELINE_TOKENS:
                # Codex's own "% context left", rounded, turned into % used so it matches the TUI
                eff = win - CODEX_BASELINE_TOKENS
                left = round(min(100, max(0, (eff - max(0, tokens - CODEX_BASELINE_TOKENS)) / eff * 100)))
                a.update(ctx_size=win, ctx_pct=100 - left)
            out.append(a)
        except Exception:
            continue  # a vanished process or unreadable state db must not break discovery
    codex_prune({a["id"] for a in out})
    return out


def codex_usage():
    """the account's 5h/week limits as of the newest rollout; a window whose reset has passed is stale, so dropped"""
    out = {}
    try:
        # the newest few: a thread that was just created may not have logged its limits yet
        rows = codex_query("SELECT rollout_path FROM threads ORDER BY updated_at_ms DESC LIMIT 3")
    except sqlite3.Error:
        return None
    for (path,) in rows:
        rl = rollout_info(path).get("rate_limits")
        if rl:
            for w in (rl.get("primary"), rl.get("secondary")):
                key = {300: "five_hour", 10080: "seven_day"}.get((w or {}).get("window_minutes"))
                if key and (w.get("resets_at") or 0) > time.time():
                    out[key] = {"pct": w.get("used_percent"), **_reset(w["resets_at"] * 1000)}
            break
    return out or None


def _epoch_ms(v):
    """ISO string or epoch seconds -> epoch ms"""
    from datetime import datetime

    if isinstance(v, (int, float)):
        return int(v * 1000)
    return int(datetime.fromisoformat(v).timestamp() * 1000)


def _next_month_ms():
    """local midnight on the first day of next month, in epoch ms"""
    t = time.localtime()
    y, m = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
    return int(time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1)) * 1000)


def _reset(ms):
    """a reset time, with the local UTC offset at that instant (the plugin has no time zone; DST may differ from now)"""
    return {"resets_at": ms, "utc_offset_s": time.localtime(ms / 1000).tm_gmtoff}


def normalize_usage(resp):
    """the usage endpoint's response -> our cache fields; the one place to adapt when the API changes"""
    out = {}
    spend, extra = resp.get("spend"), resp.get("extra_usage")
    if spend:
        if spend.get("enabled") and spend.get("limit"):
            used, limit = spend.get("used") or {}, spend["limit"]
            out["credits"] = {
                "used": used.get("amount_minor", 0) / 10 ** used.get("exponent", 0),
                "limit": limit["amount_minor"] / 10 ** limit.get("exponent", 0),
                "pct": spend.get("percent"),
                "currency": used.get("currency") or limit.get("currency"),
                **_reset(_next_month_ms()),
            }
    elif extra and extra.get("is_enabled") is not False and extra.get("monthly_limit") is not None:
        scale = 10 ** (extra.get("decimal_places") or 0)
        out["credits"] = {
            "used": (extra.get("used_credits") or 0) / scale,
            "limit": extra["monthly_limit"] / scale,
            "pct": extra.get("utilization"),
            **_reset(_next_month_ms()),
        }
    for key in ("five_hour", "seven_day"):
        w = resp.get(key)
        if w:
            out[key] = {"pct": w.get("utilization")}
            if w.get("resets_at") is not None:
                out[key].update(_reset(_epoch_ms(w["resets_at"])))
    return out or {"error": "unrecognized response"}


def fetch_usage():
    """fetch Claude's account usage into the cache; the token is only ever put in the request header"""
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None  # urllib would copy the Authorization header to the redirect target: fail with HTTP 3xx

    res = {"fetched_at": int(time.time() * 1000)}
    try:
        try:
            with open(os.path.join(CLAUDE_DIR, ".credentials.json")) as f:
                token = json.load(f)["claudeAiOauth"]["accessToken"]
        except (OSError, ValueError, KeyError, TypeError):
            token = None
        if not token:
            res["error"] = "not logged in"
        else:
            req = urllib.request.Request(
                USAGE_URL, headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
            )
            with urllib.request.build_opener(NoRedirect).open(req, timeout=10) as r:
                res.update(normalize_usage(json.load(r)))
    except urllib.error.HTTPError as e:
        res["error"] = "login expired" if e.code in (401, 403) else f"HTTP {e.code}"
    except urllib.error.URLError as e:
        res["error"] = str(e.reason)[:40]
    except Exception as e:
        res["error"] = type(e).__name__
    try:
        write_json(os.path.join(USAGE_DIR, "claude.json"), res)
    finally:
        try:
            os.remove(os.path.join(USAGE_DIR, ".fetching"))
        except OSError:
            pass


def _age_s(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def _claim_fetch(marker):
    """atomically create the fetch marker; False while another fetch holds it (one older than FETCH_WAIT_S is stale)"""
    for _ in range(2):
        try:
            os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            age = _age_s(marker)
            if age is not None and age <= FETCH_WAIT_S:
                return False
            try:
                os.remove(marker)
            except FileNotFoundError:
                pass
    return False


def claude_usage():
    """the cached account usage; when stale, refresh it in the background so a scan never waits on the network"""
    if not os.path.exists(os.path.join(CLAUDE_DIR, ".credentials.json")):
        return None  # no Claude login (e.g. Cursor only): no fetch and no usage line
    cache, marker = os.path.join(USAGE_DIR, "claude.json"), os.path.join(USAGE_DIR, ".fetching")
    age = _age_s(cache)
    if age is None or age > USAGE_MAX_AGE_S:
        try:
            os.makedirs(USAGE_DIR, exist_ok=True)
            if _claim_fetch(marker):
                subprocess.Popen(
                    [sys.executable, "-I", os.path.abspath(__file__), "--fetch-usage"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except OSError:
            pass  # a cache or spawn failure only loses the usage line, never the agent scan
    try:
        with open(cache) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def notification_text(a):
    kind = (a.get("kind") or "agent").capitalize()
    asks = a.get("status") == "needs_input"
    where = "@".join(p for p in (a.get("name"), a.get("branch")) if p)
    body = " · ".join(p for p in (where, a.get("title"), asks and a.get("detail")) if p)
    return f"{kind} needs you" if asks else f"{kind} finished", body


def send_notification(title, body):
    """start the notifier detached and don't wait: PowerShell takes ~2s and a scan must stay fast"""
    env = dict(os.environ, ZAM_TITLE=title, ZAM_BODY=body)
    script = None
    wsl = os.environ.get("WSL_DISTRO_NAME") or os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop")
    if wsl and shutil.which("powershell.exe"):
        # the Zellij server's env, inherited from the terminal it was started in
        if os.environ.get("TERM_PROGRAM") == "WezTerm":
            env["ZAM_TERM"] = "wezterm-gui"
        elif os.environ.get("WT_SESSION"):
            env["ZAM_TERM"] = "WindowsTerminal"
        else:
            env["ZAM_TERM"] = ""  # unknown terminal: always notify
        env["WSLENV"] = ":".join(filter(None, (os.environ.get("WSLENV"), "ZAM_TITLE/w:ZAM_BODY/w:ZAM_TERM/w")))
        cmd, script = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "-"], TOAST_PS
    elif shutil.which("notify-send"):
        cmd = ["notify-send", "-a", "zellij-agents", title, body]
    else:
        return
    try:
        p = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if script else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        if script:
            with p.stdin:
                p.stdin.write(script.encode())  # far below the pipe buffer, so this never blocks
    except OSError:
        pass  # a failed notification must not break the scan


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def notify_transitions(agents):
    """notify when an agent starts waiting or finishes its turn, once across all bars"""
    try:
        os.makedirs(NOTIFY_DIR, exist_ok=True)
        files = os.listdir(NOTIFY_DIR)
    except OSError:
        return
    live, stale = set(), set()
    for a in agents:
        aid, status = str(a.get("id") or ""), a.get("status")
        if not safe_id(aid):
            continue
        live.add(aid)
        since = _int(a.get("status_since"))
        cur = f"{status} {a.get('status_since')}"
        path = os.path.join(NOTIFY_DIR, aid)
        try:
            with open(path) as f:
                prev = f.read()
        except OSError:
            prev = None  # first sight (bar start, agent already waiting): only record
        if prev == cur:
            continue
        prev_status, _, prev_since = (prev or "").partition(" ")
        prev_since = _int(prev_since)
        if since is not None and prev_since is not None and since < prev_since:
            continue  # this bar's snapshot is older than the recorded state: never move it backwards
        fire = (
            prev is not None
            and a.get("hooked", True)
            and (
                (status == "needs_input" and prev_status != "needs_input")
                or (status == "idle" and prev_status in ("busy", "needs_input"))
            )
        )
        claim = f"{aid}@{status}@{a.get('status_since')}"
        try:
            if fire:
                try:
                    os.close(os.open(os.path.join(NOTIFY_DIR, claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                    send_notification(*notification_text(a))
                except FileExistsError:
                    pass  # another bar got there first
            tmp = os.path.join(NOTIFY_DIR, f".{aid}.{os.getpid()}.tmp")
            with open(tmp, "w") as f:
                f.write(cur)
            os.replace(tmp, path)
        except OSError:
            continue
        # claims of this agent's older transitions
        for n in files:
            parts = n.rsplit("@", 2)
            if len(parts) == 3 and parts[0] == aid and since is not None:
                cs = _int(parts[2])
                if cs is not None and cs < since:
                    stale.add(n)
    # files of agents not seen for a day, and tmp files left by a killed scan; a single missed scan drops nothing
    for n in files:
        if n.startswith("."):
            gone = True
        else:
            gone = n not in live and n.rsplit("@", 2)[0] not in live
        if gone:
            age = _age_s(os.path.join(NOTIFY_DIR, n))
            if age is not None and age > 86400:
                stale.add(n)
    for n in stale:
        try:
            os.remove(os.path.join(NOTIFY_DIR, n))
        except OSError:
            pass  # another bar removed it


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--seen":
        mark_seen(sys.argv[2], sys.argv[3])
        return
    if sys.argv[1:2] == ["--cursor-hook"]:
        return cursor_hook()
    if sys.argv[1:2] == ["--codex-hook"]:
        return codex_hook()
    chain = " ".join(sys.argv[3:] if sys.argv[2:3] == ["--"] else sys.argv[2:])
    if sys.argv[1:2] == ["--cursor-statusline"]:
        return cursor_statusline(chain)
    if sys.argv[1:2] == ["--claude-statusline"]:
        return claude_statusline(chain)
    if sys.argv[1:2] == ["--fetch-usage"]:
        return fetch_usage()
    agents = claude_agents() + cursor_agents() + codex_agents()
    agents.sort(key=lambda a: a.get("started_at", 0))
    if "--notify" in sys.argv:
        try:
            notify_transitions(agents)
        except Exception:
            pass  # notifications must never cost the bar its output
    usage = {k: u for k, u in (("claude", claude_usage()), ("codex", codex_usage())) if u}
    out = {
        "now": int(time.time() * 1000),
        "agents": agents,
        "usage": usage,
    }
    json.dump(out, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
