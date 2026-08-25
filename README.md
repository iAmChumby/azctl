# azctl

[![CI](https://github.com/iAmChumby/azctl/actions/workflows/ci.yml/badge.svg)](https://github.com/iAmChumby/azctl/actions/workflows/ci.yml)

**A single-file Textual TUI for the [Azurite](https://github.com/Azure/Azurite) Azure Storage emulator.**

Azurite is really three services running side by side — Blob, Queue, and Table — and out of the box there is no single place to see whether they are up, down, or broken. `azctl` is that place: a full-screen terminal dashboard that shows the health of all three services at once, lets you start and stop them, streams their logs live, and names the process squatting on a port when something else has claimed it.

```
╭─◆ azctl · Azurite Storage Emulator── host 127.0.0.1 · data ~/.azurite ──┐
╰─────────────────────────────────────────────────────────────────────────┘
╭───────────────────────────╮ ╭───────────────────────────╮ ╭─────────────
│ ▸ ● running               │ │   ● running               │ │   ◆ port in │
│ Blob · port 10000         │ │ Queue · port 10001        │ │ Table · ... │
│ pid 24188  up 0:14:32     │ │ pid 24201  up 0:14:31     │ │ pid —       │
╰───────────────────────────╯ ╰───────────────────────────╯ ╰─────────────
╭─ logs · Blob ────────────────────────────────────────────────────────────╮
│  ... live log output of the selected service ...                         │
╰──────────────────────────────────────────────────────────────────────────╯
 [Start] [ Stop ] [ Restart ] [ Save ] [ Free port ] [ Start all ] [ Stop all]
● running   ◐ starting   ○ stopped   ✖ broken   ◆ port in use
logs: selected   ↑↓ service · ←→ actions · Enter run · a all-logs · ...
```

## Zero-install run

One file, no setup. It bootstraps Textual and psutil on first run (into an isolated venv — your system Python is never touched):

```bash
curl -fsSLO https://raw.githubusercontent.com/iAmChumby/azctl/main/azctl.py
python3 azctl.py
```

Or clone and run:

```bash
git clone https://github.com/iAmChumby/azctl && cd azctl
python3 azctl.py
```

## Install as a command

Installs `azctl` as a proper command on your PATH (the app still self-bootstraps its deps into its own private venv; nothing else is touched):

**macOS / Linux / WSL** (installs to `~/.local/bin/azctl`):

```bash
curl -fsSL https://raw.githubusercontent.com/iAmChumby/azctl/main/install.sh | sh
```

or from a checkout: `./install.sh` (optionally pass a tag/branch to pin).

**Windows PowerShell** (installs to `%LOCALAPPDATA%\azctl\bin`, adds it to your user PATH, creates an `azctl.cmd` shim):

```powershell
irm https://raw.githubusercontent.com/iAmChumby/azctl/main/install.ps1 -OutFile install.ps1
.\install.ps1
```

Both scripts accept a ref argument (`./install.sh v1.2.3`) to pin a version instead of following the default branch.

Azurite itself installs with `npm install -g azurite`. If it's missing, `azctl` tells you exactly that instead of failing cryptically.

## The four ways to use it

| Command | What it does |
|---|---|
| `azctl.py` | The full-screen dashboard. The only mode that can change anything. |
| `azctl.py up` | Dashboard, but starts all three services immediately on entry. |
| `azctl.py status` | One read-only snapshot of all three services, then exits. |
| `azctl.py watch` | The same snapshot, self-refreshing. Read-only. For a side terminal. |
| `azctl.py free-ports` | Finds every process holding an Azurite port, names it, asks, kills it. |

`status` and `watch` never change anything, ever — safe to run anywhere, any time, including while a dashboard is open in another window.

## What you see

Every service gets its own **card**, always in exactly one of five states, each with its own colour and symbol so it reads at a glance:

| State | Colour | Meaning |
|---|---|---|
| ● running | green | Up and answering. |
| ◐ starting | yellow | Launched a moment ago, not answering yet (the card gently pulses). |
| ○ stopped | grey | Nothing running, nothing on the port. |
| ✖ broken | red | Launched but died, or never came up within 10 s — the card shows the exit code. |
| ◆ port in use | magenta | *Something else* owns this port. The 20-minutes-of-confusion state, called out in its own colour so it can't be mistaken for "running." |

Each card also shows the service's PID, uptime, port, and a miniature
**activity sparkline** of its recent log traffic — a busy service and a silent
one are distinguishable without reading a single line.

## How you drive it

No hair-trigger hotkeys. **↑/↓** pick a service, **←/→** move along the action bar (destructive actions are red), **Enter** runs the highlighted one. Anything that stops or kills a process asks first, in plain words, naming the exact thing about to happen — `Free port` even looks up the squatter and asks "kill node (PID 24188) on port 10000?" so you're never confirming a blind kill. Everything is mouse-friendly too: click a card to select it, click an action to run it.

Press **a** to merge all three logs into one colour-coded stream, ordered by actual arrival time — what you want when chasing a bug that crosses services. Press **/** to filter the log panel live as you type; Esc clears it. Press **q** to quit; if services are still running it asks whether to stop them or leave them up, because only you know which you meant.

Nothing in this tool can touch stored data. It manages *whether the services run* — it will never read, write, or delete a byte of what's inside them.

## Quality-of-life extras

- **Service cards with sparklines** — per-service health, PID, uptime, and a mini graph of recent log activity, all readable at a glance.
- **`?` help overlay** — every key and action explained without leaving the dashboard.
- **`c` connection strings** — shows the Azurite connection string per service and copies it to your clipboard (OSC 52, works over SSH); copy any of the others straight from the overlay.
- **`/` log filter** — narrow the log panel live as you type; the active filter shows on the panel border and in the footer.
- **Toast notifications** — significant results (started, stopped, broken, saved) also pop up as toasts so they're hard to miss.
- **Busy spinner** — the message line spins while a confirmed action is still finishing, so the app never looks frozen.
- **Bell + toast on broken** — an audible and visible cue the moment a service flips to broken, suppressed while you're in a confirmation so it never competes with a decision.
- **`status --json`** — machine-readable snapshot for scripts and CI:

  ```json
  {
    "host": "127.0.0.1",
    "data_dir": "/home/you/.azurite",
    "services": {
      "blob":  { "port": 10000, "state": "port in use", "pid": 24188 },
      "queue": { "port": 10001, "state": "stopped",     "pid": null },
      "table": { "port": 10002, "state": "stopped",     "pid": null }
    }
  }
  ```

- **Config file + flags** — custom ports, host, data directory via `~/.config/azctl/config.json` (Windows: `%APPDATA%\azctl\config.json`) or `--blob-port`-style flags.
- **Mouse support** — click a service card to select it, click an action to run it.
- **Save all** — one keystroke (`S`) writes the merged log of all three services, each line tagged with its service name and timestamp, to a single plain-text file (`azurite-all.log`).
- **Version line** — the header shows the Azurite and Node versions actually in use, so "works on my machine" arguments end faster.
- **Timestamps toggle** (`t`) — prefix every log line with its arrival time.

## Requirements

- Python 3.9+ (any OS — Linux, macOS, Windows)
- Node.js + `npm install -g azurite` (azctl will tell you if it's missing)
- Textual and psutil are bootstrapped automatically on first run if not present

## Development

```bash
pip install pytest pytest-asyncio textual psutil ruff
pytest -q          # full suite (~183 tests)
ruff check azctl.py tests/
```

A bare `pytest` on a fresh clone skips the dependency-dependent modules cleanly; the read-only command tests run with the standard library alone. CI runs lint plus the suite on Linux/macOS/Windows × Python 3.9/3.12, and cold-starts the app on a deps-less runner as a smoke test.

## Behavior spec

The complete stakeholder-facing behavioral contract lives in [BEHAVIOR.md](BEHAVIOR.md). If the app and that document disagree, it's a bug.

## License

MIT
