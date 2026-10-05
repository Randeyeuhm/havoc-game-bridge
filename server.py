#!/usr/bin/env python3
"""
Havoc Game Bridge - MCP (stdio) server + localhost HTTP hub.   (stdlib only)

    VS Code / Copilot  <--MCP stdio-->  [this process]  <--HTTP 127.0.0.1-->  game (bridge.luau)

The MCP side exposes the tools: run_luau / state / remotes / logs / bridge_status.
The HTTP side (POST /sync) is what the in-game Luau bridge polls; it uploads
console logs, remote-call logs and state snapshots, and receives queued commands.

Run:  python tools/game-bridge/server.py [--port 8722] [--token havoc-bridge]
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "bridge-1.0.0"
CONNECT_WINDOW_S = 5.0


# ----------------------------------------------------------------------------
# hub state (shared between the MCP thread and the HTTP thread)
# ----------------------------------------------------------------------------
class Hub:
    def __init__(self, token: str) -> None:
        self.token = token
        self.lock = threading.Lock()
        self.cmds: list = []                      # commands queued for games
        self.waiters: dict = {}                   # cmd id -> threading.Event
        self.results: dict = {}                   # cmd id -> result dict
        self.logs: deque = deque(maxlen=2000)     # console lines (tagged)
        self.remotes: deque = deque(maxlen=5000)  # remote call log entries
        self.state = None                         # last state snapshot (newest session)
        self.state_at = 0.0
        self.hello = None                         # newest hello payload
        self.last_sync = 0.0
        self.sync_count = 0
        # v1.1 multi-session: keyed by bridge id ("job|player")
        self.sessions: dict = {}

    def sess(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if s is None:
            s = {"id": sid, "hello": None, "state": None, "state_at": 0.0,
                 "last_sync": 0.0, "syncs": 0}
            self.sessions[sid] = s
        return s

    def prune_sessions(self, now: float) -> None:
        for k in [k for k, s in self.sessions.items() if now - s["last_sync"] > 600]:
            del self.sessions[k]

    def default_sid(self) -> str:
        best, bt = "", -1.0
        for k, s in self.sessions.items():
            if s["last_sync"] > bt:
                best, bt = k, s["last_sync"]
        return best

    @staticmethod
    def short_sid(sid: str) -> str:
        return sid.split("|")[-1] if "|" in sid else sid

    def connected(self) -> bool:
        return self.last_sync > 0 and (time.time() - self.last_sync) < CONNECT_WINDOW_S


# ----------------------------------------------------------------------------
# tools (shared by MCP tools/call and the REST /eval endpoint)
# ----------------------------------------------------------------------------
def tool_run_luau(hub: Hub, args: dict):
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return ("run_luau needs a non-empty 'code' string", True)
    with hub.lock:
        sids = list(hub.sessions.keys())
        live = [x for x in sids if hub.sessions[x]["last_sync"] and (time.time() - hub.sessions[x]["last_sync"]) < CONNECT_WINDOW_S]
    target = args.get("session")
    if isinstance(target, str) and target:
        matches = [x for x in live if target in x]
        if not matches:
            return (f"no live session matching '{target}' (live: {', '.join(live) or 'none'})", True)
        target = matches[0]
    elif len(live) > 1:
        return ("multiple live sessions - pass 'session' (a player name) to target one:\n"
                + "\n".join("  " + x for x in live), True)
    else:
        target = None
    if not hub.connected():
        return (
            "game bridge NOT connected.\n"
            "1) keep this MCP server running\n"
            "2) in the game (Volt executor) run:\n"
            "   loadstring(game:HttpGet('https://raw.githubusercontent.com/randeyeuhm/havoc-hub/main/bridge.luau'))()\n"
            "then retry.",
            True,
        )
    try:
        timeout = float(args.get("timeout_ms") or 20000) / 1000.0
    except (TypeError, ValueError):
        timeout = 20.0
    timeout = max(1.0, min(timeout, 120.0))
    cid = "c" + uuid.uuid4().hex[:10]
    ev = threading.Event()
    with hub.lock:
        hub.waiters[cid] = ev
        hub.cmds.append({"id": cid, "kind": "eval", "code": code, "target": target})
    got = ev.wait(timeout)
    with hub.lock:
        hub.waiters.pop(cid, None)
        res = hub.results.pop(cid, None)
        if not got:
            hub.cmds = [c for c in hub.cmds if c.get("id") != cid]
    if got and res is not None:
        parts = []
        prints = res.get("prints") or []
        if prints:
            parts.append("prints:\n" + "\n".join(str(p) for p in prints))
        if res.get("ok"):
            parts.append("result: " + str(res.get("out")))
        else:
            parts.append("error: " + str(res.get("err")))
        return ("\n".join(parts) or "(no output)", False)
    return (f"no result within {timeout:.1f}s - the code may still be running, or the bridge went offline", True)


def tool_state(hub: Hub, args: dict):
    want = args.get("session")
    with hub.lock:
        sids = list(hub.sessions.keys())
        if isinstance(want, str) and want:
            matches = [x for x in sids if want in x]
            if not matches:
                return (f"no session matching '{want}' (known: {', '.join(sids) or 'none'})", True)
            sid = matches[0]
        else:
            sid = hub.default_sid()
        s = hub.sessions.get(sid) or {}
        st = s.get("state")
        at = s.get("state_at") or 0.0
        conn = hub.connected()
    extra = ""
    if len(sids) > 1:
        extra = "\n# sessions: " + ", ".join(sids)
    if st is None:
        return (f"no state snapshot yet for session '{sid or '?'}'{extra}", True)
    text = json.dumps(st, indent=2, ensure_ascii=False)
    age = time.time() - at if at else -1
    header = f"# state [{sid}] (uploaded {age:.1f}s ago, connected={conn}){extra}\n"
    if len(text) > 24000:
        text = text[:24000] + "\n... (truncated - use run_luau for targeted slices)"
    return (header + text, False)


def tool_remotes(hub: Hub, args: dict):
    filt = str(args.get("filter") or "").lower()
    sess = str(args.get("session") or "").lower()
    try:
        limit = int(args.get("limit") or 40)
    except (TypeError, ValueError):
        limit = 40
    limit = max(1, min(limit, 200))
    with hub.lock:
        items = list(hub.remotes)
    out = []
    for e in reversed(items):
        name = str(e.get("name") or "")
        path = str(e.get("path") or "")
        src = str(e.get("src") or "")
        if sess and sess not in src.lower():
            continue
        if filt and filt not in name.lower() and filt not in path.lower():
            continue
        arrow = "<-" if e.get("dir") == "in" else "->"
        m = str(e.get("m") or ("OnClientEvent" if e.get("dir") == "in" else "FireServer"))
        a_s = ", ".join(str(a) for a in (e.get("args") or []))
        org = str(e.get("origin") or "")
        by = f" by {org}" if org else ""
        tag = f"[{src.split('|')[-1]}] " if src else ""
        tt = time.strftime("%H:%M:%S", time.localtime(e.get("t") or 0))
        out.append(f"{tag}[{tt}] {arrow} {m} {name} ({path}){by} :: {a_s}"[:420])
        if len(out) >= limit:
            break
    if not out:
        return (f"no remote calls captured{' matching ' + filt if filt else ''} (yet)", False)
    return ("(newest first)\n" + "\n".join(out), False)


def tool_logs(hub: Hub, args: dict):
    try:
        limit = int(args.get("limit") or 60)
    except (TypeError, ValueError):
        limit = 60
    limit = max(1, min(limit, 400))
    sess = str(args.get("session") or "").lower()
    with hub.lock:
        items = list(hub.logs)
    if sess:
        items = [x for x in items if sess in x.lower()]
    if not items:
        return ("no console output captured yet", False)
    return ("(oldest first, last %d)\n" % min(limit, len(items)) + "\n".join(items[-limit:]), False)


def tool_sessions(hub: Hub, args: dict):
    now = time.time()
    with hub.lock:
        rows = []
        for sid, s in sorted(hub.sessions.items(), key=lambda kv: -kv[1]["last_sync"]):
            live = (now - s["last_sync"]) < CONNECT_WINDOW_S if s["last_sync"] else False
            st_age = ("%0.1fs ago" % (now - s["state_at"])) if s["state_at"] else "none"
            rows.append(f"{sid}  {'LIVE' if live else 'idle'}\n"
                        f"    last sync: {now - s['last_sync']:.1f}s ago | syncs: {s['syncs']} | state: {st_age}")
    if not rows:
        return ("no sessions yet", False)
    return ("sessions (newest first):\n" + "\n".join(rows), False)


def tool_bridge_status(hub: Hub, args: dict):
    with hub.lock:
        conn = hub.connected()
        age = (time.time() - hub.last_sync) if hub.last_sync else None
        hello = hub.hello
        st_at = hub.state_at
        counts = {
            "syncs": hub.sync_count,
            "logs": len(hub.logs),
            "remotes": len(hub.remotes),
            "queued_cmds": len(hub.cmds),
            "waiters": len(hub.waiters),
            "sessions": len(hub.sessions),
        }
        sess_lines = []
        now = time.time()
        for sid, s in sorted(hub.sessions.items(), key=lambda kv: -kv[1]["last_sync"]):
            live = (now - s["last_sync"]) < CONNECT_WINDOW_S if s["last_sync"] else False
            sess_lines.append(f"  {'[LIVE]' if live else '[idle]'} {sid}")
    lines = ["bridge connected: " + str(conn)]
    if age is not None:
        lines.append(f"last sync: {age:.2f}s ago")
    lines.append("counters: " + json.dumps(counts))
    if st_at:
        lines.append(f"state age: {time.time() - st_at:.1f}s")
    if hello:
        lines.append("hello: " + json.dumps(hello, ensure_ascii=False))
    if sess_lines:
        lines.append("sessions:")
        lines.extend(sess_lines)
    if not conn:
        lines.append("TIP: execute bridge.luau in the game and keep this server running")
    return ("\n".join(lines), False)


def dispatch(hub: Hub, name: str, args: dict):
    if name == "run_luau":
        return tool_run_luau(hub, args)
    if name == "state":
        return tool_state(hub, args)
    if name == "remotes":
        return tool_remotes(hub, args)
    if name == "logs":
        return tool_logs(hub, args)
    if name == "bridge_status":
        return tool_bridge_status(hub, args)
    if name == "sessions":
        return tool_sessions(hub, args)
    return ("unknown tool: " + str(name), True)


TOOL_DEFS = [
    {
        "name": "run_luau",
        "description": (
            "Execute Luau in the LIVE game via the game bridge and return captured prints, the "
            "return value and any error. Runs client-side in the executor (getgenv) context. "
            "Use 'return ...' to send values back; keep snippets small and watch side effects. "
            "With multiple sessions connected, pass 'session' (player name) to target one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Luau source to execute."},
                "timeout_ms": {"type": "number", "description": "Wait for the result, default 20000."},
                "session": {"type": "string", "description": "Target session (player name substring). Required when multiple bridges are live."},
            },
            "required": ["code"],
        },
    },
    {
        "name": "state",
        "description": "Latest live state snapshot uploaded by the game bridge (players, attributes, remotes inventory). Optional 'session' selects one bridge.",
        "inputSchema": {
            "type": "object",
            "properties": {"session": {"type": "string", "description": "Session (player name substring); default = most recently active."}},
        },
    },
    {
        "name": "remotes",
        "description": "Recent remote-call log: outbound FireServer/InvokeServer (->) and inbound OnClientEvent/OnClientInvoke (<-), newest first. Entries are tagged with the source session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "Case-insensitive substring filter on remote name/path."},
                "session": {"type": "string", "description": "Only entries from this session (player name substring)."},
                "limit": {"type": "number", "description": "Max entries, default 40."},
            },
        },
    },
    {
        "name": "logs",
        "description": "Tail of the game console (print/warn, mirrored by the bridge). Lines are tagged with the source session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "number", "description": "Max lines, default 60."},
                "session": {"type": "string", "description": "Only lines from this session (player name substring)."},
            },
        },
    },
    {
        "name": "bridge_status",
        "description": "Bridge connectivity + counters (sync age, captured logs/remotes, pending commands) and the list of connected sessions.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sessions",
        "description": "List all bridge sessions (multi-instance aware): id, live/idle, last sync age, sync count, state age.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ----------------------------------------------------------------------------
# HTTP hub (what bridge.luau talks to)
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    hub: Hub = None  # set in main()

    def log_message(self, fmt, *args):  # silence request logging
        pass

    def _json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            hub = self.hub
            with hub.lock:
                info = {
                    "ok": True,
                    "connected": hub.connected(),
                    "last_sync_age_s": round(time.time() - hub.last_sync, 3) if hub.last_sync else None,
                    "syncs": hub.sync_count,
                    "queued_cmds": len(hub.cmds),
                }
            self._json(200, info)
        else:
            self._json(404, {"ok": False, "error": "unknown path"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._json(400, {"ok": False, "error": "bad json"})
            return
        if not isinstance(data, dict) or data.get("token") != self.hub.token:
            self._json(401, {"ok": False, "error": "bad token"})
            return
        path = self.path.split("?")[0]
        if path == "/sync":
            self._sync(data)
        elif path == "/eval":
            text, is_err = tool_run_luau(
                self.hub,
                {"code": data.get("code", ""), "timeout_ms": data.get("timeout_ms", 20000)},
            )
            self._json(200, {"ok": not is_err, "text": text})
        else:
            self._json(404, {"ok": False, "error": "unknown path"})

    def _sync(self, data: dict):
        hub = self.hub
        now = time.time()
        sid = data.get("id")
        if not isinstance(sid, str) or not sid:
            sid = "default"
        tag = hub.short_sid(sid)
        with hub.lock:
            hub.last_sync = now
            hub.sync_count += 1
            s = hub.sess(sid)
            s["last_sync"] = now
            s["syncs"] += 1
            if isinstance(data.get("hello"), dict):
                s["hello"] = data["hello"]
                hub.hello = data["hello"]
            if isinstance(data.get("state"), dict):
                s["state"] = data["state"]
                s["state_at"] = now
                hub.state = data["state"]
                hub.state_at = now
            for entry in data.get("logs") or []:
                if isinstance(entry, str):
                    hub.logs.append(f"[{tag}] {entry}")
            for entry in data.get("remotes") or []:
                if isinstance(entry, dict):
                    entry["t"] = now
                    entry["src"] = sid
                    hub.remotes.append(entry)
            for res in data.get("results") or []:
                if not isinstance(res, dict):
                    continue
                cid = res.get("id")
                ev = hub.waiters.get(cid)
                if ev is not None:
                    hub.results[cid] = res
                    ev.set()
            out_cmds, keep = [], []
            for c in hub.cmds:
                tgt = c.get("target")
                if tgt is None or tgt == sid:
                    out_cmds.append(c)
                else:
                    keep.append(c)
            hub.cmds = keep
            hub.prune_sessions(now)
        self._json(200, {"ok": True, "cmds": out_cmds})


# ----------------------------------------------------------------------------
# MCP stdio loop
# ----------------------------------------------------------------------------
def reply(mid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply_err(mid, code, message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve_stdio(hub: Hub):
    while True:
        line = sys.stdin.readline()
        if line == "":
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if not isinstance(msg, dict):
            continue
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        try:
            if method == "initialize":
                proto = params.get("protocolVersion") or "2024-11-05"
                reply(mid, {
                    "protocolVersion": proto,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "game-bridge", "version": VERSION},
                })
            elif method in ("notifications/initialized", "notifications/cancelled"):
                pass
            elif method == "ping":
                reply(mid, {})
            elif method == "tools/list":
                reply(mid, {"tools": TOOL_DEFS})
            elif method == "tools/call":
                name = params.get("name") or ""
                targs = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                text, is_err = dispatch(hub, name, targs)
                reply(mid, {"content": [{"type": "text", "text": text}], "isError": bool(is_err)})
            elif mid is not None:
                reply_err(mid, -32601, "method not found: " + str(method))
        except Exception as exc:  # never kill the loop on a bad call
            if mid is not None:
                reply_err(mid, -32603, "internal error: " + str(exc))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stdin.reconfigure(encoding="utf-8", newline="\n")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Havoc game bridge (MCP + localhost hub)")
    ap.add_argument("--port", type=int, default=8722)
    ap.add_argument("--token", default="havoc-bridge")
    args = ap.parse_args()

    hub = Hub(args.token)
    Handler.hub = hub
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(
            f"[game-bridge] cannot bind 127.0.0.1:{args.port} ({exc}) - "
            "another bridge hub may already be running; kill it or use --port.",
            file=sys.stderr,
        )
        sys.exit(1)
    threading.Thread(target=server.serve_forever, name="http-hub", daemon=True).start()
    print(
        f"[game-bridge] MCP stdio up; HTTP hub on http://127.0.0.1:{args.port} (token: {args.token})",
        file=sys.stderr,
    )
    serve_stdio(hub)


if __name__ == "__main__":
    main()
