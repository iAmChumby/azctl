# azctl

**A single-file Textual TUI for the [Azurite](https://github.com/Azure/Azurite) Azure Storage emulator.**

Azurite is really three services running side by side — Blob, Queue, and Table — and out of the box there is no single place to see whether they are up, down, or broken. `azctl` is that place: a full-screen terminal dashboard that shows the health of all three services at once, lets you start and stop them, streams their logs live, and names the process squatting on a port when something else has claimed it.

```
┌─ azctl · Azurite Storage Emulator ─ 127.0.0.1 · data: ~/.azurite ─┐
│  Service   Status        Port    PID     Uptime                   │
│ ▸Blob      ● running     10000   24188   0:14:32                  │
│  Queue     ● running     10001   24201   0:14:31                  │
│  Table     ○ stopped     10002   —       —                        │
│──────────────────────────────────────────────────────────────────│
│  ... live log output of the selected service ...                  │
│──────────────────────────────────────────────────────────────────│
│ [Start] [Stop] [Restart] [Save] [Free port] │ [Start all] [Stop all]
└──────────────────────────────────────────────────────────────────┘
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

Every service is always in exactly one of five states, each with its own colour and symbol so it reads at a glance:

| State | Colour | Meaning |
|---|---|---|
| ● running | green | Up and answering. |
| ◐ starting | yellow | Launched a moment ago, not answering yet. |
| ○ stopped | grey | Nothing running, nothing on the port. |
| ✖ broken | red | Launched but died, or never came up within 10 s. |
| ◆ port in use | magenta | *Something else* owns this port. The 20-minutes-of-confusion state, called out in its own colour so it can't be mistaken for "running." |

## How you drive it

No hair-trigger hotkeys. **↑/↓** pick a service, **←/→** move along the button bar (destructive buttons are red), **Enter** runs the highlighted button. Anything that stops or kills a process asks first, in plain words, naming the exact thing about to happen — `Free port` even looks up the squatter and asks "kill node (PID 24188) on port 10000?" so you're never confirming a blind kill.

Press **a** to merge all three logs into one colour-coded stream, ordered by actual arrival time — what you want when chasing a bug that crosses services. Press **q** to quit; if services are still running it asks whether to stop them or leave them up, because only you know which you meant.

Nothing in this tool can touch stored data. It manages *whether the services run* — it will never read, write, or delete a byte of what's inside them.

## Quality-of-life extras

- **`?` help overlay** — every key and button explained without leaving the dashboard.
- **`c` connection strings** — shows the Azurite connection string per service and copies it to your clipboard (OSC 52, works over SSH).
- **`status --json`** — machine-readable snapshot for scripts and CI.
- **Config file + flags** — custom ports, host, data directory via `~/.config/azctl/config.json` or `--blob-port`-style flags.
- **Mouse support** — click a row to select it.
- **Terminal bell on failure** — an audible cue the moment a service flips to broken, so you notice even when the window isn't focused.
- **Save all** — one keystroke (`S`) writes the merged log of all three services, each line tagged with its service name and timestamp, to a single plain-text file (`azurite-all.log`).
- **Version line** — the header shows the Azurite and Node versions actually in use, so "works on my machine" arguments end faster.
- **Timestamps toggle** (`t`) — prefix every log line with its arrival time.

## Requirements

- Python 3.9+ (any OS — Linux, macOS, Windows)
- Node.js + `npm install -g azurite` (azctl will tell you if it's missing)
- Textual and psutil are bootstrapped automatically on first run if not present

## Behavior spec

The complete stakeholder-facing behavioral contract lives in [BEHAVIOR.md](BEHAVIOR.md). If the app and that document disagree, it's a bug.

## License

MIT
