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
| `run_luau` | runs Luau in the live client; returns prints, return value, errors |
| `state` | latest snapshot: players + attributes, your attrs/char, remote inventory |
| `remotes` | live remote-call log — `->` FireServer/InvokeServer (via `__namecall`), `<-` OnClientEvent/OnClientInvoke, args serialised, newest first, filterable |
| `logs` | tail of the game console (print/warn mirrored) |
| `bridge_status` | connectivity + counters |

## Notes / troubleshooting

- **"game bridge NOT connected"** → the in-game script isn't running, or the hub
  isn't up. The bridge retries every 0.4s, so just start the missing side.
- **Port 8722 busy** → a stale hub process is running; kill it (`Get-Process python`)
  or start both sides with a matching `--port` / `BRIDGE.URL`.
- **Token** — defaults to `havoc-bridge` on both sides (localhost-only, but change
  it in `server.py --token` + `BRIDGE.TOKEN` if you share the machine).
- **Remote log volume** — ring buffers: 400 entries in-game, 5000 in the hub.
  A heavy session can wrap them; filter early.
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
