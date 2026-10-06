#!/usr/bin/env python3
"""
End-to-end smoke test for the game bridge (server.py).

Starts server.py as a real subprocess, talks MCP over its stdio, and plays
the part of TWO game clients with fake /sync loops. Covers:

  1. MCP handshake + tools/list (run_luau, state, remotes, logs, bridge_status, sessions)
  2. single session: bridge_status connected, run_luau round trip, remotes filter, logs
  3. REST: POST /eval round trip, GET /health (live_sessions, drained queues)
  4. multi-session: untargeted run_luau refused; targeted run lands on the right client
  5. REST /eval with session targeting
  6. auth: 401 wrong token, 400 bad json
  7. timeout path: a code the client never answers -> clear timeout error, server healthy after

Run:  python smoke_test.py
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

PORT = 8799  # test-only port so a real hub on 8722 is never touched
TOKEN = "smoke-token"
HERE = os.path.dirname(os.path.abspath(__file__))


def http_post(path, obj=None, raw=None):
    data = raw if raw is not None else json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def http_get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


class FakeGame:
    """Acts like bridge.luau: syncs, executes eval cmds, posts results."""

    def __init__(self, sid, executor="smoke"):
        self.sid = sid
        self.executor = executor
        self.executed = []   # codes this client ran
        self.ignored = []    # codes it deliberately never answered
        self.stop = threading.Event()
        self.last_err = None
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _loop(self):
        while not self.stop.is_set():
            try:
                _, resp = http_post("/sync", {
                    "token": TOKEN,
                    "id": self.sid,
                    "hello": {"executor": self.executor, "version": "bridge-v2.4.4"},
                    "state": {"fake": True, "sid": self.sid},
                    "logs": ["[smoke] hello from " + self.sid],
                    "remotes": [{"dir": "out", "m": "FireServer", "name": "WeaponFired",
                                "path": "RS.Remotes", "args": ["Rattler"]}],
                })
                for cmd in resp.get("cmds", []):
                    if cmd.get("kind") != "eval":
                        continue
                    code = cmd["code"]
                    self.executed.append(code)
                    if "NEVER_ANSWER" in code:
                        self.ignored.append(code)
                        continue  # no result -> exercises the timeout path
                    http_post("/sync", {
                        "token": TOKEN,
                        "id": self.sid,
                        "results": [{
                            "id": cmd["id"],
                            "ok": True,
                            "out": "42:" + self.sid,
                            "prints": ["fake executed: " + code],
                        }],
                    })
            except Exception as exc:
                self.last_err = repr(exc)
            time.sleep(0.15)

    def stop_it(self):
        self.stop.set()


def main():
    srv = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "server.py"), "--port", str(PORT), "--token", TOKEN],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )

    def mcp(obj):
        srv.stdin.write(json.dumps(obj) + "\n")
        srv.stdin.flush()

    def mcp_read():
        line = srv.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout: " + srv.stderr.read())
        return json.loads(line)

    def call_tool(mid, name, args):
        mcp({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
             "params": {"name": name, "arguments": args}})
        return mcp_read()["result"]["content"][0]["text"]

    def wait_health(pred, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                h = http_get("/health")
                if pred(h):
                    return h
            except Exception:
                pass
            time.sleep(0.1)
        return None

    problems = []
    a = FakeGame("jobA|Alice").start()
    b = None
    try:
        if wait_health(lambda h: h.get("ok") and h.get("connected") and h.get("live_sessions") == 1) is None:
            problems.append("hub never saw the first session as live")

        # 1) MCP handshake
        mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "smoke", "version": "1"}}})
        init = mcp_read()
        if init["result"]["serverInfo"]["name"] != "game-bridge":
            problems.append("bad serverInfo: " + json.dumps(init))
        mcp({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 2) tools/list
        mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = [t["name"] for t in mcp_read()["result"]["tools"]]
        for want in ("run_luau", "state", "remotes", "logs", "bridge_status", "sessions"):
            if want not in tools:
                problems.append("missing tool: " + want)

        # 3) bridge_status + sessions show the live client
        status = call_tool(3, "bridge_status", {})
        if "connected: True" not in status or "Alice" not in status:
            problems.append("bridge_status missing session/connection:\n" + status)
        sess = call_tool(4, "sessions", {})
        if "jobA|Alice" not in sess or "LIVE" not in sess:
            problems.append("sessions tool missed Alice:\n" + sess)

        # 4) run_luau round trip (single session, no targeting needed)
        text = call_tool(5, "run_luau", {"code": "return 1 + 1", "timeout_ms": 5000})
        if "42:jobA|Alice" not in text or "fake executed" not in text:
            problems.append("run_luau round trip failed:\n" + text)

        # 5) remotes + logs
        rem = call_tool(6, "remotes", {"filter": "weapon"})
        if "WeaponFired" not in rem:
            problems.append("remotes filter missed entry:\n" + rem)
        logs = call_tool(7, "logs", {"limit": 5})
        if "[smoke] hello from jobA|Alice" not in logs:
            problems.append("logs missing entry:\n" + logs)

        # 6) REST /eval single session
        code_rest = "return 'rest-single'"
        sc, body = http_post("/eval", {"token": TOKEN, "code": code_rest, "timeout_ms": 5000})
        if sc != 200 or body.get("ok") is not True or "42:" not in json.dumps(body):
            problems.append(f"/eval single failed: {sc} {body}")
        if code_rest not in a.executed:
            problems.append("/eval code never reached client A")

        # ---- SECOND session joins ----
        b = FakeGame("jobB|Bob", executor="smoke2").start()
        if wait_health(lambda h: h.get("live_sessions") == 2) is None:
            problems.append("hub never saw both sessions as live")

        # 7) untargeted run_luau refused, both listed
        text = call_tool(8, "run_luau", {"code": "return 'nope'"})
        if "multiple live sessions" not in text or "Alice" not in text or "Bob" not in text:
            problems.append("multi-session refusal missing/wrong:\n" + text)

        # 8) targeted run_luau lands on Alice only
        marker = "return 'alice-only'"
        text = call_tool(9, "run_luau", {"code": marker, "session": "Alice", "timeout_ms": 5000})
        if "42:jobA|Alice" not in text:
            problems.append("targeted run_luau did not hit Alice:\n" + text)
        if marker not in a.executed or marker in b.executed:
            problems.append("targeted run_luau went to the wrong client(s)")

        # 9) REST /eval with session targeting: Bob
        marker2 = "return 'bob-only'"
        sc, body = http_post("/eval", {"token": TOKEN, "code": marker2, "session": "Bob", "timeout_ms": 5000})
        if sc != 200 or "42:jobB|Bob" not in json.dumps(body):
            problems.append(f"/eval session target failed: {sc} {body}")
        if marker2 not in b.executed or marker2 in a.executed:
            problems.append("/eval session targeting went to the wrong client(s)")

        # 10) auth: 401 wrong token, 400 bad json
        sc, body = http_post("/eval", {"token": "wrong", "code": "return 1"})
        if sc != 401 or body.get("error") != "bad token":
            problems.append(f"bad token not rejected: {sc} {body}")
        sc, body = http_post("/eval", raw=b"{not json")
        if sc != 400 or body.get("error") != "bad json":
            problems.append(f"bad json not rejected: {sc} {body}")

        # 11) timeout path: client never answers; clear error, server stays healthy
        t0 = time.time()
        text = call_tool(10, "run_luau", {"code": "NEVER_ANSWER from A", "session": "Alice", "timeout_ms": 1500})
        took = time.time() - t0
        if "no result within" not in text:
            problems.append("timeout path gave no clear error:\n" + text)
        if took > 4.0:
            problems.append(f"timeout took too long: {took:.1f}s")
        if "NEVER_ANSWER from A" not in a.ignored:
            problems.append("timeout test code never reached client A")

        # 12) still healthy after the timeout: round trip works, queues drain, no waiter leak
        text = call_tool(11, "run_luau", {"code": "return 1 + 1", "session": "Alice", "timeout_ms": 5000})
        if "42:" not in text:
            problems.append("post-timeout run_luau broken:\n" + text)
        if wait_health(lambda x: x.get("queued_cmds") == 0, timeout=3.0) is None:
            problems.append("queues never drained after timeout")
        st = call_tool(12, "bridge_status", {})
        if '"waiters": 0' not in st:
            problems.append("waiter leak after timeout:\n" + st)

        # 13) /health shape
        h = http_get("/health")
        if not h.get("ok") or h.get("live_sessions") != 2 or len(h.get("sessions", [])) < 2:
            problems.append("health shape wrong: " + json.dumps(h))
    finally:
        a.stop_it()
        if b:
            b.stop_it()
        try:
            srv.kill()
        except Exception:
            pass

    if problems:
        print("SMOKE FAILED:")
        for p in problems:
            print(" -", p)
        if a.last_err:
            print(" fake A last error:", a.last_err)
        if b and b.last_err:
            print(" fake B last error:", b.last_err)
        print(" server stderr:\n" + (srv.stderr.read() if srv.stderr else ""))
        sys.exit(1)
    print("SMOKE OK - MCP + REST, single + multi session, targeting, auth, timeout path all good")


if __name__ == "__main__":
    main()
