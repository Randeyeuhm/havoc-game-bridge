# Game Bridge — live MCP link into the game

Lets the Copilot agent (or anything speaking MCP) **execute Luau in the live game**
and **watch remote traffic + state** while it happens.

```
VS Code / Copilot  <--MCP stdio-->  server.py  <--HTTP 127.0.0.1:8722-->  bridge.luau (in game)
```

Python **stdlib only** — no pip installs. Works with any executor that has an HTTP
function (`request` / `http_request`) and a `__namecall` hook for the remote log
(Volt has both; without the hook everything else still works).

> Extracted from [`havoc-hub`](https://github.com/Randeyeuhm/havoc-hub) (history
> preserved). The in-game side (`bridge.luau`) lives in that repo.

## Setup (2 steps)

1. **Start the MCP server** — VS Code: Command Palette → **MCP: List Servers** →
   `game-bridge` → **Start**. The auto-start config ships in `.vscode/mcp.json`
   of this repo (portable, uses `${workspaceFolder}`), so opening this folder in
   VS Code is enough:

   ```json
   {
     "servers": {
       "game-bridge": {
         "type": "stdio",
         "command": "python",
         "args": ["${workspaceFolder}/server.py"]
       }
     }
   }
   ```

   Manual run also works: `python server.py`

2. **Start the in-game bridge** — in the game (Volt), execute:

   ```lua
   loadstring(game:HttpGet("https://raw.githubusercontent.com/randeyeuhm/havoc-hub/main/bridge.luau"))()
   ```

Then ask the agent to use the tools. Stop the in-game side any time with
`getgenv().HAVOC_BRIDGE.alive = false` (re-executing also retires the old one).

## Tools the agent gets

| tool | what it does |
|---|---|
| `run_luau` | runs Luau in the live client (or a local `.luau` via `file`); returns prints, return value, errors, round-trip ms |
| `state` | latest snapshot: players + attributes, your attrs/char, remote inventory |
| `remotes` | live remote-call log — `->` FireServer/InvokeServer (via `__namecall`), `<-` OnClientEvent/OnClientInvoke, args serialised, newest first, filterable |
| `logs` | tail of the game console (print/warn mirrored) |
| `bridge_status` | connectivity + counters |
| `sessions` | list every bridge session (multi-instance aware): id, live/idle, last sync age, syncs, state age |

## REST (for scripts / no-MCP clients)

- `GET /health` — **open** (no token): `ok`, `connected`, `last_sync_age_s`, `syncs`, `queued_cmds`, `live_sessions`, `sessions[]`, `eval_ms_last`, `eval_ms_avg`
- `POST /eval` — body `{"token": "...", "code": "...", "timeout_ms": 20000, "session": "Alice", "file": "C:/path/x.luau"}` — same code path as `run_luau`; `session` targets one client when several are live; `file` runs a local `.luau` file's contents instead of `code`. Responses end with `round trip: <n> ms`.
- `GET /logs?limit=60&session=&token=…` — tail of the mirrored console (print/warn) — same as the `logs` tool
- `GET /remotes?filter=&session=&limit=40&token=…` — remote-call log — same as the `remotes` tool
- `GET /state?session=&token=…` — latest state snapshot — same as the `state` tool
- `GET /sessions?token=…` — connected bridge sessions — same as the `sessions` tool
- **Auth:** every GET except `/health` requires the token (`?token=` or `X-Havoc-Token` header); requests carrying a foreign `Origin` are rejected (403) — a browser page cannot read the bridge.

## Game-side kit (bridge v2.5.0, in-game script)

- **Spy pack** — `HAVOC_BRIDGE.spy.byName(name, limit)` (filter the ring), `spy.dump([path], [nameFilter])` (**writes the ring to a workspace file** — read it from disk, no console paste; binary strings come out hexed), `spy.hex(s)` for buffer payloads, `raw(i)` / `replay(i)` as before.
- **Watch** — `HAVOC_BRIDGE.watch(instanceOrPath, seconds, interval)` samples properties + attributes and returns a diff log ("what changes when I do X"). Blocks the call: give the eval a matching `timeout_ms`.
- **Teleport persistence** — the bridge re-queues itself (`queueonteleport`) so it survives server hops (lobby → match, races). Disable with `getgenv().HAVOC_BRIDGE_PERSIST = false`; status in `HAVOC_BRIDGE.persist`.
- **Console + async rings** — `HAVOC_BRIDGE.console` (last 150 print/warn/[bridge] lines) and `HAVOC_BRIDGE.asyncErrors` (errors from eval-spawned `task.spawn/defer/delay`, zero writes to the real task table — shadow-task prefix).
- **Eval hardening** — `evalResults` (last 10 results, recoverable if a post is lost), `evalsDone` / `lastExecMs` / `droppedResults` counters, chunk name `@bridge_eval` in error traces.
- **Adaptive polling** — fast ticks (~0.12 s) for a few seconds after any command → eval round trips ~0.3 s while active, 0.45 s idle.
- **Caps map** — `state.health.caps`: 24 executor capabilities probed at boot (readfile, hookmetamethod, getgc, queueonteleport, ...), so you know a game's abilities at a glance.

## Notes / troubleshooting

- **"game bridge NOT connected"** → the in-game script isn't running, or the hub
  isn't up. The bridge retries every 0.4s, so just start the missing side.
- **Port 8722 busy** → a stale hub process is running; kill it (`Get-Process python`)
  or run the hub on another port (`--port 8799`) and point the bridge at it with
  `getgenv().HAVOC_BRIDGE_URL = "http://127.0.0.1:8799"` before re-running it (v2.4.4).
- **Token** — defaults to `havoc-bridge` on both sides (localhost-only, but change
  it in `server.py --token` + the in-game override `getgenv().HAVOC_BRIDGE_TOKEN`
  if you share the machine).
- **Remote log volume** — in-game caps: 80-entry replay ring + 500 queued remotes
  + 400 queued logs; hub rings: 5000 remotes / 2000 logs. A heavy session wraps
  them; filter early.
- **Multi-session** — several clients can sync to one hub at once; tools take
  `session` (player-name substring). Untargeted calls refuse when more than one
  client is live, and ambiguous names are rejected with the match list. `sessions`
  (tool) or `/health` (REST) shows who is connected.
- **Endpoint overrides (v2.4.4)** — the in-game bridge reads
  `getgenv().HAVOC_BRIDGE_URL` and `getgenv().HAVOC_BRIDGE_TOKEN` at boot; set
  them before running to point at a non-default port / second hub / custom token
  without editing any file.
- **Remote log safety (v1.1)** — the `__namecall` hook only queues raw packets;
  all serialization happens on the sync thread. If an outbound remote ever
  misbehaves while logging is on, flip the live kill switch:
  `getgenv().HAVOC_BRIDGE.remoteLog = false` (takes effect for new fires; the
  rest of the bridge keeps working). Incident log: the v1 hook design silently
  ate `Match.Deploy` when a second namecall hook layer (Infinite Yield) was
  loaded in the same session — fixed in v1.1 by removing all instance access
  and nested calls from inside the hook.
- **Live remote discovery (v1.2)** — remotes are watched the moment they appear
  anywhere in the tree (`game.DescendantAdded` + one async full sweep +
  `getnilinstances()`), not just the ones under `ReplicatedStorage` at boot.
  Same pattern as the Dex RemoteSpy and Cobalt builds.
- **Caller attribution (v1.3)** — outbound entries include the script that
  fired them (`getcallingscript`, captured in the hook exactly like Cobalt
  does), shown as `by <script>` in the `remotes` tool.
- **Crash-proofing (v1.4)** — non-finite numbers (`nan`/`inf`, common in
  Criminality-style games) can no longer kill the sync loop: finite guards in
  the state builder and serializer, the state build is pcall'd, and an encode
  failure strips the state and keeps syncing instead of dying.
- **Security** — the hub binds 127.0.0.1 only, and the only credential is the
  token. Anything that can reach localhost and knows the token can run code in
  your game client. Dev place only, don't run this on public servers.

## Test

```powershell
python server.py --port 8799 --token smoke   # optional manual
python smoke_test.py                         # full end-to-end
```
