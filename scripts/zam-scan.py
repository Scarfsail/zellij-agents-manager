#!/usr/bin/env python3
"""Discover running coding agents and the zellij pane each one lives in.

Prints a JSON array on stdout; the zellij plugin runs this on a timer.

Claude Code: every interactive process writes ~/.claude/sessions/<pid>.json
(status busy/idle/shell, cwd, name, startedAt). We keep the live ones, read
ZELLIJ_SESSION_NAME / ZELLIJ_PANE_ID from /proc/<pid>/environ, take context
usage from the tail of the transcript (window size from Claude's model catalog cache), and overlay the last hook event
(written by zam-hook.sh) to detect "waiting for permission".
"""
import glob
import json
import os
import re
import socket
import stat
import struct
import sys
import time

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR", os.path.join(HOME, ".claude"))
HOOK_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "zellij-agents", "claude")
# "read" marks for questions: <session_id> holds the status_since of the question the user has looked at
SEEN_DIR = os.path.join(os.path.dirname(HOOK_DIR), "seen")
TAIL_BYTES = 256 * 1024


def seen_ts(sid):
    try:
        with open(os.path.join(SEEN_DIR, sid)) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def mark_seen(sid, ts):
    if not sid or "/" in sid or sid.startswith("."):
        return
    os.makedirs(SEEN_DIR, exist_ok=True)
    tmp = os.path.join(SEEN_DIR, f".{sid}.tmp")
    with open(tmp, "w") as f:
        f.write(str(int(ts)))
    os.replace(tmp, os.path.join(SEEN_DIR, sid))


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


def transcript_info(path):
    """Context tokens + model (last assistant usage), session title, git branch."""
    info = {}
    if not path:
        return info
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            _scan_lines(reversed(f.read().splitlines()), info)
            if size > TAIL_BYTES and not ({"custom_title", "ai_title"} & info.keys()):
                f.seek(0)  # title written early and not repeated since: scan the whole file once
                _scan_lines(reversed(f.read().splitlines()), info)
    except OSError:
        pass
    # Claude: custom-title (/rename) beats ai-title; both are last-wins
    info["title"] = info.pop("custom_title", None) or info.pop("ai_title", None) or ""
    info.pop("ai_title", None)
    return info


def hook_state(session_id):
    try:
        with open(os.path.join(HOOK_DIR, f"{session_id}.json")) as f:
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
            # ZELLIJ_SESSION_NAME is frozen at process start; a renamed session would jump to the old name
            "zellij_session": zellij_session_of(pid, env.get("ZELLIJ_SESSION_NAME")) if env.get("ZELLIJ_SESSION_NAME") else None,
            "pane_id": int(env["ZELLIJ_PANE_ID"]) if env.get("ZELLIJ_PANE_ID", "").isdigit() else None,
            "version": s.get("version"),
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


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--seen":
        mark_seen(sys.argv[2], sys.argv[3])
        return
    agents = claude_agents()
    # future: codex_agents(), cursor_agents(), ...
    agents.sort(key=lambda a: a.get("started_at", 0))
    json.dump({"now": int(time.time() * 1000), "agents": agents}, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
