#!/usr/bin/env python3
"""
End-to-end smoke test for the game bridge. Starts server.py as a real
subprocess, talks MCP over its stdio, and plays the part of the game with a
fake /sync client - then calls the run_luau tool and checks the round trip.

Run:  python tools/game-bridge/smoke_test.py
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

PORT = 8799  # test-only port so a real hub on 8722 is never touched
TOKEN = "smoke-token"
HERE = os.path.dirname(os.path.abspath(__file__))


def http_post(path, obj):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    srv = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "server.py"), "--port", str(PORT), "--token", TOKEN],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    stop = threading.Event()
    executed = []
    last_err = []

    def fake_game():
        """Acts like bridge.luau: syncs, executes eval cmds, posts results."""
        while not stop.is_set():
            try:
                resp = http_post("/sync", {
                    "token": TOKEN,
                    "hello": {"executor": "smoke", "version": "bridge-v1"},
                    "state": {"fake": True, "players": ["tester"]},
                    "logs": ["[smoke] hello"],
                    "remotes": [{"dir": "out", "m": "FireServer", "name": "WeaponFired", "path": "RS.Remotes", "args": ["Rattler"]}],
                })
                last_err.clear()
                for cmd in resp.get("cmds", []):
                    if cmd.get("kind") == "eval":
                        executed.append(cmd["code"])
                        http_post("/sync", {
                            "token": TOKEN,
                            "results": [{
                                "id": cmd["id"],
                                "ok": True,
                                "out": "42",
                                "prints": ["fake-game executed: " + cmd["code"]],
                            }],
                        })
            except Exception as exc:
                last_err[:] = [repr(exc)]
            time.sleep(0.15)

    th = threading.Thread(target=fake_game, daemon=True)
    th.start()

    # wait for the hub to accept connections before asserting anything
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1) as r:
                if json.loads(r.read().decode("utf-8")).get("ok"):
                    break
        except Exception:
            pass
        time.sleep(0.1)
    time.sleep(0.5)

    def mcp(obj):
        srv.stdin.write(json.dumps(obj) + "\n")
        srv.stdin.flush()

    def mcp_read():
        line = srv.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout: " + srv.stderr.read())
        return json.loads(line)

    problems = []
    try:
        # 1) MCP handshake
        mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}}})
        init = mcp_read()
        assert init["result"]["serverInfo"]["name"] == "game-bridge", init
        mcp({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 2) tools/list
        mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = [t["name"] for t in mcp_read()["result"]["tools"]]
        for want in ("run_luau", "state", "remotes", "logs", "bridge_status"):
            if want not in tools:
                problems.append("missing tool: " + want)

        # 3) bridge_status should see the fake game
        mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "bridge_status", "arguments": {}}})
        status = mcp_read()["result"]["content"][0]["text"]
        if "connected: True" not in status:
            problems.append("bridge_status not connected:\n" + status)

        # 4) run_luau round trip through the fake game
        mcp({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "run_luau", "arguments": {"code": "return 1 + 1", "timeout_ms": 5000}}})
        text = mcp_read()["result"]["content"][0]["text"]
        if "42" not in text or "fake-game executed" not in text:
            problems.append("run_luau round trip failed:\n" + text)
        if not executed:
            problems.append("fake game never received the eval command")

        # 5) remotes + logs endpoints
        mcp({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "remotes", "arguments": {"filter": "weapon"}}})
        rem = mcp_read()["result"]["content"][0]["text"]
        if "WeaponFired" not in rem:
            problems.append("remotes filter missed the entry:\n" + rem)

        mcp({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "logs", "arguments": {"limit": 5}}})
        logs = mcp_read()["result"]["content"][0]["text"]
        if "[smoke] hello" not in logs:
            problems.append("logs missing entry:\n" + logs)

        # 6) REST /health
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        if not health.get("ok"):
            problems.append("/health not ok: " + json.dumps(health))
    finally:
        stop.set()
        try:
            srv.kill()
        except Exception:
            pass

    if problems:
        print("SMOKE FAILED:")
        for p in problems:
            print(" -", p)
        if last_err:
            print(" fake game last error:", last_err[0])
        print(" server stderr:\n" + (srv.stderr.read() if srv.stderr else ""))
        sys.exit(1)
    print("SMOKE OK - MCP handshake, tools, bridge sync, run_luau round trip, remotes/logs/health all good")


if __name__ == "__main__":
    main()
