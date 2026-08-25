#!/usr/bin/env python3
"""azctl — a single-file terminal dashboard for the Azurite storage emulator.

Behavioral contract: BEHAVIOR.md in this repository.

The file is one self-bootstrapping application: if its two dependencies
(textual, psutil) are not importable, it creates a private venv under the
user cache directory, installs them there, and re-execs itself with that
venv's python. Only the standard library runs before that point.
"""

from __future__ import annotations

# --- stdlib imports only ------------------------------------------------ §0
import argparse
import asyncio
import base64
import collections
import itertools
import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

# --- constants ---------------------------------------------------------- §1
SERVICE_ORDER = ("blob", "queue", "table")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_DATA_DIR = "~/.azurite"

RUNNING = "running"
STARTING = "starting"
STOPPED = "stopped"
BROKEN = "broken"
PORT_IN_USE = "port in use"

STATE_SYMBOLS = {
    RUNNING: "●",      # ●
    STARTING: "◐",     # ◐
    STOPPED: "○",      # ○
    BROKEN: "✖",       # ✖
    PORT_IN_USE: "◆",  # ◆
}
STATE_COLOURS = {
    RUNNING: "green",
    STARTING: "yellow",
    STOPPED: "grey50",
    BROKEN: "red",
    PORT_IN_USE: "magenta",
}
SERVICE_COLOURS = {"blob": "cyan", "queue": "green", "table": "magenta"}

START_DEADLINE = 10.0
LOG_CAPACITY = 2000
MESSAGE_TTL = 4.0
BOOTSTRAP_SENTINEL = "AZCTL_BOOTSTRAPPED"
INSTALL_HINT = "npm install -g azurite"

__version__ = "1.0.0"

# Version floors for the private-venv bootstrap. Textual breaks APIs between
# major versions (this file is written against the 8.x API), and an unpinned
# `pip install textual` would let a future major release break every fresh
# install the day it ships — so constrain to a known-good range instead.
TEXTUAL_SPEC = "textual>=8,<9"
PSUTIL_SPEC = "psutil>=5.7"
DEP_SPECS = (TEXTUAL_SPEC, PSUTIL_SPEC)

# The well-known Azurite development account (public constants, not secrets).
ACCOUNT_NAME = "devstoreaccount1"
ACCOUNT_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)


# --- dependency probe + bootstrap (stdlib only) ------------------------- §2
def bootstrap_venv_dir(environ, home, is_windows) -> pathlib.Path:
    """Pure path logic for where the private venv lives."""
    if is_windows:
        base = environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    else:
        base = environ.get("XDG_CACHE_HOME") or ""
        if not base:
            base = os.path.join(home, ".cache")
    return pathlib.Path(base) / "azctl" / "venv"


def bootstrap_venv_python(venv_dir, is_windows) -> pathlib.Path:
    if is_windows:
        return pathlib.Path(venv_dir) / "Scripts" / "python.exe"
    return pathlib.Path(venv_dir) / "bin" / "python"


def _venv_has_pip(vpy, runner=subprocess.run) -> bool:
    """True when vpy exists and its own `-m pip --version` succeeds.

    A venv can exist on disk yet be structurally unusable forever (created
    with `--without-pip`, or on a Debian/Ubuntu system missing python3-venv's
    ensurepip prerequisite) — vpy.exists() alone can't tell those apart from
    a healthy venv, so every bootstrap would fail identically on every run.
    """
    if not pathlib.Path(vpy).exists():
        return False
    try:
        probe = runner([str(vpy), "-m", "pip", "--version"], capture_output=True, text=True)
    except Exception:  # noqa: BLE001 - treat any failure as "not usable"
        return False
    return probe.returncode == 0


def _deps_importable(vpy, runner=subprocess.run) -> bool:
    """True when `import textual, psutil` succeeds inside vpy's interpreter.

    pip's exit code alone is not proof of a working install: a corrupted
    package (dist-info intact, payload missing/quarantined) makes pip report
    "already satisfied" and exit 0 without reinstalling anything.
    """
    try:
        probe = runner([str(vpy), "-c", "import textual, psutil"], capture_output=True, text=True)
    except Exception:  # noqa: BLE001
        return False
    return probe.returncode == 0


def _bootstrap_explain(venv_dir, vpy, detail="") -> None:
    lines = ["azctl could not set up its dependencies (textual and psutil)."]
    if detail:
        lines += ["", detail.rstrip()]
    lines += [
        "",
        "azctl keeps them in a private virtualenv (your system Python is never touched):",
        "    " + str(venv_dir),
        "",
        "The usual causes are no network access, a missing/broken pip, a corrupted",
        "virtualenv, or (on Debian/Ubuntu) a system Python missing python3-venv.",
        "You can finish the setup manually with:",
        "    " + str(vpy) + " -m pip install " + " ".join('"%s"' % s for s in DEP_SPECS),
        "If that reports \"already satisfied\" but azctl still can't import them,",
        "the install itself may be corrupted — force a clean reinstall with:",
        "    " + str(vpy) + " -m pip install --force-reinstall --no-cache-dir textual psutil",
        "and if the virtualenv is broken beyond that (e.g. no pip at all), delete it",
        "and let azctl rebuild it from scratch:",
        "    rm -rf " + str(venv_dir),
        "or install textual and psutil into any Python and re-run azctl with that python.",
    ]
    print("\n".join(lines), file=sys.stderr)


def _bootstrap_and_reexec() -> None:
    """Create the private venv, install deps, re-exec. Never returns."""
    is_windows = os.name == "nt"
    venv_dir = bootstrap_venv_dir(os.environ, os.path.expanduser("~"), is_windows)
    vpy = bootstrap_venv_python(venv_dir, is_windows)

    if os.environ.get(BOOTSTRAP_SENTINEL) == "1":
        _bootstrap_explain(
            venv_dir,
            vpy,
            "azctl already tried to install them once this run, but textual/psutil "
            "are still not importable from the private virtualenv.",
        )
        raise SystemExit(1)

    if not _venv_has_pip(vpy):
        # Covers "never created" and "exists but broken/pip-less" alike (e.g.
        # `python3 -m venv --without-pip`, or a Debian/Ubuntu system missing
        # python3-venv/ensurepip) — recreate from scratch instead of retrying
        # a pip install that would fail identically on every future run.
        if venv_dir.exists():
            shutil.rmtree(str(venv_dir), ignore_errors=True)
        print(
            "azctl: first run — setting up a private environment in %s "
            "(this can take a few seconds)..." % venv_dir,
            file=sys.stderr,
            flush=True,
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                capture_output=True,
                text=True,
            )
        except Exception as exc:  # noqa: BLE001 - must never traceback here
            _bootstrap_explain(venv_dir, vpy, "Creating the virtualenv failed: %s" % exc)
            raise SystemExit(1)
        if proc.returncode != 0:
            _bootstrap_explain(
                venv_dir,
                vpy,
                "Creating the virtualenv failed:\n" + (proc.stderr or proc.stdout or "").strip(),
            )
            raise SystemExit(1)
        if not _venv_has_pip(vpy):
            _bootstrap_explain(
                venv_dir,
                vpy,
                "The virtualenv was created but has no usable pip — a common symptom "
                "on Debian/Ubuntu systems missing the python3-venv or python3-pip "
                "packages. Install those with your system package manager (or "
                "delete the virtualenv above and ensure `python3 -m ensurepip` "
                "works), then re-run azctl.",
            )
            raise SystemExit(1)

    # stderr + flush: keeps stdout clean for `status --json` and survives execve.
    print(
        "azctl: installing textual and psutil into %s ..." % venv_dir,
        file=sys.stderr,
        flush=True,
    )
    try:
        proc = subprocess.run(
            [str(vpy), "-m", "pip", "install", "--quiet", *DEP_SPECS],
            capture_output=True,
            text=True,
        )
    except Exception as exc:  # noqa: BLE001
        _bootstrap_explain(venv_dir, vpy, "Running pip failed: %s" % exc)
        raise SystemExit(1)
    if proc.returncode != 0:
        combined = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        tail = "\n".join(combined.splitlines()[-15:])
        _bootstrap_explain(venv_dir, vpy, "pip install failed:\n" + tail)
        raise SystemExit(1)

    if not _deps_importable(vpy):
        # pip exited 0 but the packages aren't actually importable: retry
        # once with a forced, cache-busting reinstall before giving up.
        try:
            proc = subprocess.run(
                [
                    str(vpy), "-m", "pip", "install", "--quiet",
                    "--force-reinstall", "--no-cache-dir", *DEP_SPECS,
                ],
                capture_output=True,
                text=True,
            )
        except Exception as exc:  # noqa: BLE001
            _bootstrap_explain(venv_dir, vpy, "Running pip failed: %s" % exc)
            raise SystemExit(1)
        if proc.returncode != 0 or not _deps_importable(vpy):
            combined = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
            tail = "\n".join(combined.splitlines()[-15:])
            _bootstrap_explain(
                venv_dir,
                vpy,
                "textual/psutil are still not importable even after a forced "
                "reinstall:\n" + tail,
            )
            raise SystemExit(1)

    env = dict(os.environ)
    env[BOOTSTRAP_SENTINEL] = "1"
    argv = [str(vpy), os.path.abspath(__file__)] + sys.argv[1:]
    if is_windows:
        raise SystemExit(subprocess.call(argv, env=env))
    os.execve(str(vpy), argv, env)


# --- guarded third-party import ----------------------------------------- §3
try:
    import psutil
    from rich.console import Console, Group
    from rich.live import Live  # still used by cmd_watch (frozen, read-only)
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.css.query import NoMatches
    from textual.message import Message
    from textual.screen import ModalScreen
    from textual.containers import VerticalScroll
    from textual.widgets import Input, RichLog, Static

    _HAVE_DEPS = True
except ImportError:
    psutil = None
    Console = Group = Live = Panel = Table = Text = None
    work = App = ComposeResult = Binding = Message = ModalScreen = None
    Horizontal = Vertical = VerticalScroll = None
    Input = RichLog = Static = NoMatches = None
    _HAVE_DEPS = False


# --- config ------------------------------------------------------------- §4
@dataclass
class Config:
    host: str = DEFAULT_HOST
    blob_port: int = 10000
    queue_port: int = 10001
    table_port: int = 10002
    data_dir: str = DEFAULT_DATA_DIR

    def port_for(self, service: str) -> int:
        return getattr(self, service + "_port")


def config_path(environ, home, is_windows) -> pathlib.Path:
    if is_windows:
        base = environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    else:
        base = environ.get("XDG_CONFIG_HOME") or ""
        if not base:
            base = os.path.join(home, ".config")
    return pathlib.Path(base) / "azctl" / "config.json"


def load_config(path) -> dict:
    """Read the config file. Missing → {}. Broken → warn on stderr, {}."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print("azctl: warning: could not read config file %s (%s); ignoring it." % (path, exc), file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print("azctl: warning: config file %s is not a JSON object; ignoring it." % path, file=sys.stderr)
        return {}
    out = {}
    for key in ("host", "data_dir"):
        if key in data:
            out[key] = str(data[key])
    for key in ("blob_port", "queue_port", "table_port"):
        if key in data:
            try:
                out[key] = int(data[key])
            except (TypeError, ValueError):
                print("azctl: warning: config key %r is not a port number; skipping it." % key, file=sys.stderr)
    return out


def resolve_config(args, file_dict) -> Config:
    """defaults < config file < CLI flags."""
    cfg = Config()
    for key in ("host", "blob_port", "queue_port", "table_port", "data_dir"):
        if key in file_dict:
            setattr(cfg, key, file_dict[key])
        cli_val = getattr(args, key, None)
        if cli_val is not None:
            setattr(cfg, key, cli_val)
    cfg.data_dir = os.path.expanduser(cfg.data_dir)
    return cfg


# --- engine: logs, service, ServiceManager ------------------------------ §5
@dataclass
class LogLine:
    seq: int
    mono: float
    wall: float
    service: str
    text: str


@dataclass
class ServiceView:
    name: str
    state: str
    port: int
    pid: "int | None"
    uptime: "float | None"
    ever_started: bool
    exit_code: "int | None"


@dataclass
class Transition:
    service: str
    old: str
    new: str


class LogStore:
    """Ring buffers per service plus one merged buffer in true arrival order.

    A single global sequence counter is taken under the same lock as the
    appends, so seq is strictly increasing across services and the merged
    deque is by construction sorted by arrival.
    """

    def __init__(self, capacity: int = LOG_CAPACITY) -> None:
        self._lock = threading.Lock()
        self._seq = itertools.count()
        self._per = {name: collections.deque(maxlen=capacity) for name in SERVICE_ORDER}
        self._merged = collections.deque(maxlen=3 * capacity)

    def append(self, service: str, text: str) -> None:
        with self._lock:
            line = LogLine(next(self._seq), time.monotonic(), time.time(), service, text)
            self._per[service].append(line)
            self._merged.append(line)

    def lines(self, service: str) -> "list[LogLine]":
        with self._lock:
            return list(self._per[service])

    def merged(self) -> "list[LogLine]":
        with self._lock:
            return list(self._merged)


class _Svc:
    __slots__ = (
        "name", "proc", "launched_at", "stopping", "state", "ever_started",
        "exit_code", "reader", "broken_reason",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        self.proc = None
        self.launched_at = None
        self.stopping = False
        self.state = STOPPED
        self.ever_started = False
        self.exit_code = None
        self.reader = None
        self.broken_reason = None


def port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wrap_windows_shim(exe: str) -> "list[str]":
    """npm installs .cmd/.bat batch shims on Windows (no shebang mechanism);
    CreateProcess can't exec a batch script directly (WinError 193), so it
    must be run through cmd /c — the same trick Node's cross-spawn uses."""
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe]
    return [exe]


def default_command_for(name: str, config: Config) -> "list[str] | None":
    """Resolve the azurite-<service> executable; None when not installed."""
    exe = shutil.which("azurite-" + name)
    if exe is None:
        return None
    port = config.port_for(name)
    return _wrap_windows_shim(exe) + [
        "--%sHost" % name, config.host,
        "--%sPort" % name, str(port),
        "--location", config.data_dir,
        "--silent",
    ]


class ServiceManager:
    """Headless engine: owns child processes, computes the five states.

    Imports nothing from rich; every external effect is injectable so the
    test suite can drive it without Azurite, real ports, or real time.
    """

    def __init__(
        self,
        config: Config,
        *,
        command_for=None,
        clock=time.monotonic,
        start_deadline: float = START_DEADLINE,
        health_timeout: float = 0.25,
        log_capacity: int = LOG_CAPACITY,
        spawner=subprocess.Popen,
    ) -> None:
        self.config = config
        self.command_for = command_for or default_command_for
        self.clock = clock
        self.start_deadline = start_deadline
        self.health_timeout = health_timeout
        self.spawner = spawner
        self.logs = LogStore(capacity=log_capacity)
        self._svcs = {name: _Svc(name) for name in SERVICE_ORDER}

    # -- state ----------------------------------------------------------
    def _compute_state(self, svc: _Svc) -> str:
        port = self.config.port_for(svc.name)
        proc = svc.proc
        if proc is not None:
            rc = proc.poll()
            if rc is None:
                if port_open(self.config.host, port, self.health_timeout):
                    return RUNNING
                if svc.launched_at is not None and self.clock() - svc.launched_at < self.start_deadline:
                    return STARTING
                return BROKEN  # launched, never came up (recovers if it answers later)
            if svc.stopping:
                # We asked it to die; not broken. Fall through to rule 3.
                svc.proc = None
                svc.launched_at = None
                svc.stopping = False
            else:
                # Launched then died on its own: sticky BROKEN until Start/Stop.
                svc.exit_code = rc
                svc.launched_at = None
                if rc == 127:
                    # The realistic "Node missing" shape: the kernel execs the
                    # #!/usr/bin/env node shebang script fine (FileNotFoundError
                    # never fires), then `env` itself fails to resolve `node`
                    # and the child exits 127 within milliseconds. Surface the
                    # same clear message Popen's FileNotFoundError path uses,
                    # instead of a silent, unexplained BROKEN.
                    svc.broken_reason = (
                        "Node.js runtime not found — install Node, then %s" % INSTALL_HINT
                    )
                else:
                    svc.broken_reason = None
                return BROKEN
        if port_open(self.config.host, port, self.health_timeout):
            return PORT_IN_USE
        return STOPPED

    def refresh(self) -> "list[Transition]":
        transitions = []
        for name in SERVICE_ORDER:
            svc = self._svcs[name]
            new = self._compute_state(svc)
            if new != svc.state:
                transitions.append(Transition(name, svc.state, new))
                svc.state = new
        return transitions

    def views(self) -> "list[ServiceView]":
        out = []
        for name in SERVICE_ORDER:
            svc = self._svcs[name]
            alive = svc.proc is not None and svc.proc.poll() is None
            pid = svc.proc.pid if alive else None
            uptime = None
            if alive and svc.launched_at is not None:
                uptime = self.clock() - svc.launched_at
            out.append(
                ServiceView(name, svc.state, self.config.port_for(name), pid, uptime, svc.ever_started, svc.exit_code)
            )
        return out

    def broken_reason(self, name: str) -> "str | None":
        """A distinguishing explanation for a BROKEN death (e.g. Node
        missing), or None for a generic/unexplained death or non-BROKEN
        state. Cleared by Start/Stop/Free-port like the rest of the record."""
        return self._svcs[name].broken_reason

    # -- log plumbing ----------------------------------------------------
    def _read_output(self, name: str, proc) -> None:
        stream = getattr(proc, "stdout", None)
        if stream is None:
            return
        try:
            for raw in stream:
                self.logs.append(name, raw.rstrip("\r\n"))
        except (OSError, ValueError):
            pass

    # -- lifecycle -------------------------------------------------------
    def start(self, name: str) -> "tuple[bool, str, str]":
        svc = self._svcs[name]
        title = name.capitalize()
        port = self.config.port_for(name)
        if svc.proc is not None and svc.proc.poll() is None:
            word = "running" if port_open(self.config.host, port, self.health_timeout) else "starting"
            return False, "%s is already %s." % (title, word), "yellow"
        if port_open(self.config.host, port, self.health_timeout):
            return False, "Port %d is in use by another process — use Free port." % port, "red"
        cmd = self.command_for(name, self.config)
        if cmd is None:
            return False, "Azurite is not installed — %s" % INSTALL_HINT, "red"
        try:
            os.makedirs(os.path.expanduser(self.config.data_dir), exist_ok=True)
        except OSError as exc:
            return False, "Could not create data dir %s: %s" % (self.config.data_dir, exc), "red"
        kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "text": True,
            "errors": "replace",
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = self.spawner(cmd, **kwargs)
        except FileNotFoundError:
            return False, "Node.js runtime not found — install Node, then %s" % INSTALL_HINT, "red"
        except OSError as exc:
            return False, "Could not launch %s: %s" % (title, exc), "red"
        svc.proc = proc
        svc.launched_at = self.clock()
        svc.ever_started = True
        svc.stopping = False
        svc.exit_code = None
        svc.broken_reason = None
        svc.state = STARTING
        svc.reader = threading.Thread(target=self._read_output, args=(name, proc), daemon=True)
        svc.reader.start()
        return True, "Started %s (PID %d)." % (title, proc.pid), "green"

    def stop(self, name: str) -> "tuple[bool, str, str]":
        svc = self._svcs[name]
        title = name.capitalize()
        proc = svc.proc
        if proc is None or proc.poll() is not None:
            # Nothing live we own; also clears a sticky BROKEN-died record.
            svc.proc = None
            svc.launched_at = None
            svc.stopping = False
            svc.broken_reason = None
            svc.state = self._compute_state(svc)
            return True, "%s is not running." % title, "grey"
        svc.stopping = True
        kill_pid(proc.pid, timeout=3.0)
        try:
            proc.wait(timeout=3.0)
        except Exception:
            pass
        svc.proc = None
        svc.launched_at = None
        svc.stopping = False
        svc.broken_reason = None
        if svc.reader is not None:
            svc.reader.join(timeout=1.0)
            svc.reader = None
        svc.state = self._compute_state(svc)
        return True, "Stopped %s." % title, "green"

    def restart(self, name: str) -> "tuple[bool, str, str]":
        svc = self._svcs[name]
        if svc.proc is not None and svc.proc.poll() is None:
            self.stop(name)
        return self.start(name)

    def start_all(self) -> "list[tuple[bool, str, str]]":
        return [self.start(name) for name in SERVICE_ORDER]

    def stop_all(self) -> "list[tuple[bool, str, str]]":
        return [self.stop(name) for name in SERVICE_ORDER]

    def shutdown(self) -> None:
        """Stop every service we own. Safe to call more than once."""
        for name in SERVICE_ORDER:
            svc = self._svcs[name]
            if svc.proc is not None and svc.proc.poll() is None:
                self.stop(name)

    def owns_live(self, name: str) -> bool:
        """True while we own a live child process for this service."""
        svc = self._svcs[name]
        return svc.proc is not None and svc.proc.poll() is None

    def any_owned(self) -> bool:
        return any(self.owns_live(name) for name in SERVICE_ORDER)

    def detach_all(self) -> None:
        """Forget our children without killing them (quit-and-leave-running)."""
        for svc in self._svcs.values():
            svc.proc = None
            svc.launched_at = None
            svc.stopping = False

    def notice_external_kill(self, name: str) -> None:
        """Our own child was killed via Free port: show stopped, not broken."""
        svc = self._svcs[name]
        svc.proc = None
        svc.launched_at = None
        svc.stopping = False
        svc.exit_code = None
        svc.broken_reason = None

    # -- log saving ------------------------------------------------------
    def save_service_log(self, name: str, directory=None) -> "tuple[bool, str, str]":
        lines = self.logs.lines(name)
        # os.getcwd() itself raises FileNotFoundError when the directory
        # azctl was launched from has since been removed — keep it inside
        # the same guard as the file write so that's a red message too.
        try:
            resolved_dir = directory or os.getcwd()
            path = os.path.abspath(os.path.join(resolved_dir, "azurite-%s.log" % name))
            with open(path, "w", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(line.text + "\n")
        except OSError as exc:
            return False, "Could not save the %s log: %s" % (name, exc), "red"
        if lines:
            return True, "Wrote %d lines to %s" % (len(lines), path), "green"
        return True, "Wrote 0 lines to %s (log was empty)" % path, "yellow"

    def save_merged_log(self, directory=None) -> "tuple[bool, str, str]":
        lines = self.logs.merged()
        try:
            resolved_dir = directory or os.getcwd()
            path = os.path.abspath(os.path.join(resolved_dir, "azurite-all.log"))
            with open(path, "w", encoding="utf-8") as fh:
                for line in lines:
                    stamp = time.strftime("%H:%M:%S", time.localtime(line.wall))
                    fh.write("%s [%s] %s\n" % (stamp, line.service, line.text))
        except OSError as exc:
            return False, "Could not save the merged log: %s" % exc, "red"
        if lines:
            return True, "Wrote %d lines to %s" % (len(lines), path), "green"
        return True, "Wrote 0 lines to %s (log was empty)" % path, "yellow"


# --- ports: pid lookup, kill, free-ports core --------------------------- §6
def _proc_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except Exception:
        return "unknown"


def pid_on_port(port: int) -> "tuple[int | None, str] | None":
    """(pid, name) of the LISTEN holder of `port`; None when no listener found.

    (None, "unknown") means: a listener exists but its pid is unresolvable.
    """
    if psutil is None:
        return None
    saw_listener = False
    try:
        conns = psutil.net_connections(kind="tcp")
    except Exception:
        conns = None
    if conns is not None:
        for conn in conns:
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                saw_listener = True
                if conn.pid:
                    return conn.pid, _proc_name(conn.pid)
    if conns is None or saw_listener:
        # Fallback (e.g. macOS non-root): per-process connection scan.
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                get_conns = getattr(proc, "net_connections", None) or proc.connections
                pconns = get_conns(kind="tcp")
            except Exception:
                continue
            for conn in pconns:
                if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                    return proc.info["pid"], proc.info.get("name") or "unknown"
        if saw_listener:
            return None, "unknown"
    return None


def kill_pid(pid: int, timeout: float = 2.0) -> bool:
    """Terminate a process tree (children first-class): terminate → wait → kill."""
    if psutil is None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return True
    try:
        root = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True
    procs = []
    try:
        procs = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    procs.append(root)
    for proc in procs:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # AccessDenied (e.g. a root-owned process on a shared machine)
            # must never propagate out of here and crash the TUI — the
            # caller's post-kill port recheck already reports "did not die"
            # honestly when the process survives.
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return True


def free_port_flow(
    port: int,
    confirm_cb,
    *,
    host: str = DEFAULT_HOST,
    health_timeout: float = 0.25,
    sleep=time.sleep,
    on_killed=None,
) -> "tuple[bool, str, str]":
    """Shared core of the free-ports command and the dashboard Free-port button.

    confirm_cb(prompt) -> bool decides the kill; sleep is injectable so tests
    skip the 1.5 s settle wait. on_killed(pid) fires after a confirmed kill.
    """
    if not port_open(host, port, health_timeout):
        return True, "Nothing is listening on port %d." % port, "grey"
    holder = pid_on_port(port)
    if holder is None or holder[0] is None:
        return (
            False,
            "Port %d is in use but the owning process could not be identified "
            "(try again with elevated privileges)." % port,
            "red",
        )
    pid, name = holder
    if not confirm_cb("Kill %s (PID %d) on port %d?" % (name, pid, port)):
        return False, "Cancelled.", "grey"
    kill_pid(pid)
    if on_killed is not None:
        on_killed(pid)
    sleep(1.5)
    if not port_open(host, port, health_timeout):
        return True, "Killed %s (PID %d); port %d is free." % (name, pid, port), "green"
    new_holder = pid_on_port(port)
    if new_holder is not None and new_holder[0] == pid:
        return False, "Port %d is still in use by %s (PID %d) — the process did not die." % (port, name, pid), "red"
    new_pid, new_name = (new_holder if new_holder is not None else (None, "unknown"))
    return (
        False,
        "Port %d is still in use by %s (PID %s) — something restarted it "
        "(a supervisor or editor extension, most likely). Stop it at the source."
        % (port, new_name, new_pid if new_pid is not None else "?"),
        "red",
    )


# --- probes & helpers: versions, connection strings, OSC52 -------------- §7
def format_uptime(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return "%d:%02d:%02d" % (hours, minutes, secs)


def _run_version(executable: str) -> "str | None":
    exe = shutil.which(executable)
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            _wrap_windows_shim(exe) + ["--version"], capture_output=True, text=True, timeout=3
        )
    except Exception:
        return None
    text = (proc.stdout or proc.stderr or "").strip()
    if not text:
        return None
    return text.splitlines()[0].strip()


def detect_versions() -> "tuple[str, str]":
    """(azurite version, node version); 'unknown' on any failure, never raises."""
    azurite = _run_version("azurite") or _run_version("azurite-blob") or "unknown"
    node = _run_version("node") or "unknown"
    return azurite, node


def connection_string(service: str, config: Config) -> str:
    port = config.port_for(service)
    return (
        "DefaultEndpointsProtocol=http;"
        "AccountName=%s;"
        "AccountKey=%s;"
        "%sEndpoint=http://%s:%d/%s;"
        % (ACCOUNT_NAME, ACCOUNT_KEY, service.capitalize(), config.host, port, ACCOUNT_NAME)
    )


def copy_osc52(text: str, stream=None) -> None:
    """Best-effort clipboard copy via OSC 52 (no-op on terminals that ignore it)."""
    stream = stream if stream is not None else sys.__stdout__
    if stream is None:
        return
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    try:
        stream.write("\x1b]52;c;" + payload + "\x07")
        stream.flush()
    except (OSError, ValueError):
        pass


# --- read-only commands: status / watch / free-ports CLI ---------------- §8
def observe(config: Config) -> dict:
    """Read-only snapshot. An outside observer can only honestly report
    'port in use' or 'stopped' — it owns nothing (BEHAVIOR.md)."""
    services = {}
    for name in SERVICE_ORDER:
        port = config.port_for(name)
        if port_open(config.host, port):
            holder = pid_on_port(port)
            pid = holder[0] if holder is not None else None
            state = PORT_IN_USE
        else:
            pid = None
            state = STOPPED
        services[name] = {"port": port, "state": state, "pid": pid}
    return {"host": config.host, "data_dir": config.data_dir, "services": services}


def _status_renderable(config: Config):
    snap = observe(config)
    table = Table(title="azctl · %s · data: %s" % (snap["host"], snap["data_dir"]))
    table.add_column("Service")
    table.add_column("Status")
    table.add_column("Port", justify="right")
    table.add_column("PID", justify="right")
    for name in SERVICE_ORDER:
        info = snap["services"][name]
        state = info["state"]
        table.add_row(
            name.capitalize(),
            Text("%s %s" % (STATE_SYMBOLS[state], state), style=STATE_COLOURS[state]),
            str(info["port"]),
            str(info["pid"]) if info["pid"] is not None else "—",
        )
    return table


def _print_plain_status(config: Config) -> None:
    snap = observe(config)
    print("azctl · host %s · data %s" % (snap["host"], snap["data_dir"]))
    for name in SERVICE_ORDER:
        info = snap["services"][name]
        pid = info["pid"] if info["pid"] is not None else "-"
        print("%-6s %-12s port %-6d pid %s" % (name, info["state"], info["port"], pid))


def cmd_status(config: Config, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps(observe(config), indent=2))
        return 0
    if _HAVE_DEPS:
        Console().print(_status_renderable(config))
    else:
        _print_plain_status(config)
    return 0


def cmd_watch(config: Config, interval: float = 1.0) -> int:
    try:
        if _HAVE_DEPS and sys.stdout.isatty():
            console = Console()
            with Live(_status_renderable(config), console=console, auto_refresh=False) as live:
                while True:
                    time.sleep(interval)
                    live.update(_status_renderable(config), refresh=True)
        else:
            while True:
                _print_plain_status(config)
                sys.stdout.flush()
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        # Whoever was reading us went away; leave without a traceback.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0
    try:
        print("Stopped watching. Services were left exactly as they were.")
    except OSError:
        pass
    return 0


def cmd_free_ports(config: Config, assume_yes: bool = False, service=None) -> int:
    names = [service] if service else list(SERVICE_ORDER)
    targets = [(name, config.port_for(name)) for name in names]

    occupied = []
    for name, port in targets:
        if port_open(config.host, port):
            holder = pid_on_port(port)
            pid, pname = holder if holder is not None else (None, "unknown")
            occupied.append((name, port, pid, pname))

    if not occupied:
        ports_txt = ", ".join(str(port) for _, port in targets)
        print("Nothing is listening on port(s) %s. Nothing to do." % ports_txt)
        return 0

    if _HAVE_DEPS:
        table = Table(title="Processes holding Azurite ports")
        for col in ("Service", "Port", "PID", "Process"):
            table.add_column(col)
        for name, port, pid, pname in occupied:
            table.add_row(name, str(port), str(pid) if pid is not None else "—", pname)
        Console().print(table)
    else:
        for name, port, pid, pname in occupied:
            print("%-6s port %-6d pid %-8s %s" % (name, port, pid if pid is not None else "-", pname))

    if assume_yes:
        def confirm(_prompt: str) -> bool:
            return True
    else:
        def confirm(prompt: str) -> bool:
            try:
                answer = input(prompt + " [y/N] ")
            except EOFError:
                return False
            return answer.strip().lower() in ("y", "yes")

    for name, port, _pid, _pname in occupied:
        _ok, message, _style = free_port_flow(port, confirm, host=config.host)
        print(message)

    all_free = all(not port_open(config.host, port) for _, port in targets)
    return 0 if all_free else 1


# --- TUI: UIState, pure render functions, widgets, screens, AzctlApp ----- §9
@dataclass
class Button:
    label: str
    action: str
    danger: bool
    group: int


MSG_STYLES = {"green": "green", "red": "bold red", "yellow": "yellow", "grey": "grey50"}

BUTTONS = (
    Button("Start", "start", False, 0),
    Button("Stop", "stop", True, 0),
    Button("Restart", "restart", True, 0),
    Button("Save", "save", False, 0),
    Button("Free port", "free_port", True, 0),
    Button("Start all", "start_all", False, 1),
    Button("Stop all", "stop_all", True, 1),
)

SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
SPARK_CAPACITY = 48
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class UIState:
    selected: int = 0
    button: int = 0
    combined_logs: bool = False
    timestamps: bool = False
    filter_text: str = ""
    message: "tuple | None" = None  # (text, style, expires_at)
    versions: "tuple" = ("unknown", "unknown")


def render_sparkline(counts) -> str:
    """Mini activity graph of recent log throughput; '' when there is no data."""
    if not counts:
        return ""
    peak = max(max(counts), 1)
    top = len(SPARK_BLOCKS) - 1
    return "".join(SPARK_BLOCKS[min(top, c * top // peak)] for c in counts)


def render_header(config: Config, versions) -> "Panel":
    azurite_ver, node_ver = versions
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(" ◆ ", style="bold magenta")
    text.append("azctl", style="bold cyan")
    text.append(" · Azurite Storage Emulator", style="bold")
    text.append("   host %s · data %s" % (config.host, config.data_dir))
    text.append("   azurite %s · node %s" % (azurite_ver, node_ver), style="grey50")
    return Panel(text, border_style="cyan")


def _legend_text() -> "Text":
    text = Text(no_wrap=True, overflow="ellipsis")
    for state in (RUNNING, STARTING, STOPPED, BROKEN, PORT_IN_USE):
        if len(text) > 0:
            text.append("   ")
        text.append("%s %s" % (STATE_SYMBOLS[state], state), style=STATE_COLOURS[state])
    return text


def btn_label(btn: Button, hot: bool) -> "Text":
    text = Text(no_wrap=True)
    if hot:
        text.append("[%s]" % btn.label, style=("bold red reverse" if btn.danger else "bold reverse"))
    else:
        text.append("[ %s ]" % btn.label, style=("red" if btn.danger else "grey70"))
    return text


def render_footer(ui: UIState, now: float, busy_spinner: str = "") -> "Group":
    rows = [_legend_text()]
    hints = Text(no_wrap=True, overflow="ellipsis")
    hints.append("logs: ", style="grey50")
    if ui.combined_logs:
        hints.append("ALL", style="bold yellow")
    else:
        hints.append("selected", style="grey50")
    if ui.filter_text:
        hints.append("   filter: ", style="grey50")
        hints.append("/%s/" % ui.filter_text, style="bold cyan")
    hints.append(
        "   ↑↓ service · ←→ actions · Enter run · a all-logs · t times · "
        "/ filter · c conn str · S save all · ? help · q quit",
        style="grey50",
    )
    rows.append(hints)
    if ui.message is not None and ui.message[2] > now:
        rows.append(
            Text(
                (busy_spinner + " " if busy_spinner else "") + ui.message[0],
                style=MSG_STYLES.get(ui.message[1], ""),
                no_wrap=True,
                overflow="ellipsis",
            )
        )
    elif busy_spinner:
        rows.append(Text(busy_spinner + " working…", style="bold cyan", no_wrap=True))
    else:
        rows.append(Text(""))
    return Group(*rows)


HELP_ROWS = (
    ("↑ / ↓", "Select a service — the highlighted card and the log panel follow instantly"),
    ("← / →", "Move the highlight along the action bar"),
    ("Enter", "Run the highlighted action"),
    ("a", "Toggle the merged view of all three logs (colour-tagged, arrival order)"),
    ("t", "Toggle timestamps on log lines"),
    ("/", "Open the log filter bar; type to narrow lines live, Esc clears it"),
    ("c", "Show connection strings; the selected one is copied (OSC 52)"),
    ("S", "Save the merged all-services log to azurite-all.log"),
    ("?", "This help overlay"),
    ("Esc", "Cancel a confirmation / clear the filter / close an overlay"),
    ("q", "Quit — asks what to do with services that are still running"),
    ("Ctrl+C / Ctrl+Q", "Same as q — asks what to do with services that are still running"),
    ("mouse", "Click a service card to select it; click an action to run it"),
    ("Ctrl+P", "Command palette (built into Textual)"),
    ("", ""),
    ("Start", "Start the selected service"),
    ("Stop", "Stop the selected service (asks first)"),
    ("Restart", "Stop then start the selected service (asks first)"),
    ("Save", "Write the selected service's log buffer to ./azurite-<service>.log"),
    ("Free port", "Kill whatever holds the selected service's port (asks, names it)"),
    ("Start all", "Start all three services"),
    ("Stop all", "Stop all three services (asks first)"),
)


def render_help() -> "Panel":
    table = Table(box=None, show_header=False, padding=(0, 2, 0, 0), pad_edge=False)
    table.add_column("key", style="bold cyan", no_wrap=True)
    table.add_column("what it does")
    for key, desc in HELP_ROWS:
        table.add_row(key, desc)
    return Panel(table, title="help — press any key to close", title_align="left")


def render_conn(config: Config, selected: int) -> "Text":
    text = Text()
    for i, name in enumerate(SERVICE_ORDER):
        marker = "▸ " if i == selected else "  "
        text.append("%s%s\n" % (marker, name.capitalize()), style="bold " + SERVICE_COLOURS[name])
        text.append("    %s\n\n" % connection_string(name, config))
    text.append(
        "The selected service's string was copied to the clipboard via OSC 52.\n"
        "Esc closes · click a COPY button to copy another service.",
        style="grey50",
    )
    return text


# The TUI classes below subclass Textual base classes at class-body-
# evaluation time -- all guarded-None when textual isn't importable. Gating
# the whole block behind _HAVE_DEPS means a deps-less process (status/watch/
# free-ports/--help) never executes `class Foo(None):`.
if _HAVE_DEPS:

    class ServiceCard(Static):
        """One service's health at a glance; click to select."""

        can_focus = False

        class Clicked(Message):
            def __init__(self, service: str) -> None:
                super().__init__()
                self.service = service

        def __init__(self, service: str, **kwargs) -> None:
            super().__init__(classes="service-card", **kwargs)
            self.service = service

        def on_click(self) -> None:
            self.post_message(self.Clicked(self.service))

    class ActionButton(Static):
        """One entry of the keyboard-driven action bar; also clickable."""

        can_focus = False

        class Triggered(Message):
            def __init__(self, index: int) -> None:
                super().__init__()
                self.index = index

        def __init__(self, index: int, btn: Button) -> None:
            super().__init__(classes="action-button")
            self.index = index
            self.btn = btn

        def on_click(self) -> None:
            self.post_message(self.Triggered(self.index))

    class _ModalBody(Static):
        """Static content, but focusable.

        Textual routes key events along the *focused widget's* ancestor chain;
        pushing a ModalScreen does not by itself move focus into it, so a
        modal whose body can't hold focus would never see the next keypress.
        """

        can_focus = True

        def on_mount(self) -> None:
            self.focus()

    class HelpScreen(ModalScreen):
        """Read-only overlay; any key closes it."""

        DEFAULT_CSS = """
        HelpScreen { align: center middle; }
        HelpScreen #help_scroll {
            width: 100; max-width: 92%; height: auto; max-height: 85%;
            background: $surface; border: round $primary; padding: 0 1;
        }
        """

        def compose(self) -> ComposeResult:
            with VerticalScroll(id="help_scroll"):
                yield _ModalBody(render_help())

        def on_key(self, event) -> None:
            event.stop()
            self.dismiss()

    class ConnScreen(ModalScreen):
        """Connection strings for all three services, with copy actions."""

        DEFAULT_CSS = """
        ConnScreen { align: center middle; }
        ConnScreen #conn_dialog {
            width: 100%; max-width: 110; height: auto;
            background: $surface; border: round $primary; padding: 0 1;
        }
        ConnScreen #conn_choices { height: auto; align-horizontal: center; }
        ConnScreen ActionButton { width: auto; margin: 0 1 1 1; padding: 0 2; }
        """

        def __init__(self, config: Config, selected: int) -> None:
            super().__init__()
            self.config = config
            self.selected = selected

        def compose(self) -> ComposeResult:
            with Vertical(id="conn_dialog"):
                yield _ModalBody(render_conn(self.config, self.selected))
                with Horizontal(id="conn_choices"):
                    for i, name in enumerate(SERVICE_ORDER):
                        yield ActionButton(i, Button("Copy " + name.capitalize(), "copy", False, 0))

        def on_key(self, event) -> None:
            event.stop()
            self.dismiss()

        def on_action_button_triggered(self, event: "ActionButton.Triggered") -> None:
            event.stop()
            name = SERVICE_ORDER[event.index]
            copy_osc52(connection_string(name, self.config))
            self.app.show("Copied %s connection string to clipboard." % name.capitalize(), "green")

    class ConfirmScreen(ModalScreen):
        """dismiss(True) on Enter; dismiss(False) on literally anything else.

        BEHAVIOR.md: "The question appears in the footer." ModalScreen's own
        DEFAULT_CSS only dims the background -- it does not position content
        -- so the body is docked to the bottom, at the footer's own height.
        """

        DEFAULT_CSS = """
        ConfirmScreen #modal_body { dock: bottom; height: 4; width: 100%; }
        """

        def __init__(self, prompt: str) -> None:
            super().__init__()
            self.prompt = prompt

        def compose(self) -> ComposeResult:
            yield _ModalBody(
                Text(
                    self.prompt + "  — Enter: yes · Esc or any other key: cancel",
                    style="bold yellow",
                    no_wrap=True,
                    overflow="ellipsis",
                ),
                id="modal_body",
            )

        def on_key(self, event) -> None:
            event.stop()
            self.dismiss(event.key == "enter")

    class QuitDialog(Vertical):
        """Focusable host for the three-way quit question.

        Without a focused widget inside the modal, key events keep flowing to
        whatever held focus on the screen underneath and QuitScreen.on_key is
        never reached (same reason _ModalBody exists).
        """

        can_focus = True

        def on_mount(self) -> None:
            self.focus()

    class QuitScreen(ModalScreen):
        """Three-way goodbye: Enter/'Stop & quit', n/'Leave running', Esc/'Stay'.

        Every OTHER key is swallowed and the screen stays open — three answers
        only, which is deliberately NOT ConfirmScreen's rule (BEHAVIOR.md).
        """

        DEFAULT_CSS = """
        QuitScreen { align: center middle; }
        QuitScreen #quit_dialog {
            width: 64; height: auto; background: $surface;
            border: round yellow; padding: 1 2;
        }
        QuitScreen #quit_title { text-style: bold; color: yellow; margin-bottom: 1; }
        QuitScreen #quit_choices { height: auto; align-horizontal: center; margin-top: 1; }
        QuitScreen ActionButton { width: auto; margin: 0 1; padding: 0 2; }
        """

        def compose(self) -> ComposeResult:
            with QuitDialog(id="quit_dialog"):
                yield Static("Services are still running.", id="quit_title")
                yield Static("What should happen to them when the dashboard closes?")
                with Horizontal(id="quit_choices"):
                    yield ActionButton(0, Button("Stop & quit", "stop", False, 0))
                    yield ActionButton(1, Button("Leave running", "detach", False, 0))
                    yield ActionButton(2, Button("Stay", "stay", False, 0))

        def on_key(self, event) -> None:
            event.stop()
            if event.key == "enter":
                self.dismiss("stop")
            elif event.key == "n":
                self.dismiss("detach")
            elif event.key == "escape":
                self.dismiss("stay")
            # else: deliberately ignored — three answers only, screen stays open

        def on_action_button_triggered(self, event: "ActionButton.Triggered") -> None:
            event.stop()
            self.dismiss(event.btn.action)

    class AzctlApp(App):
        """Owns UIState + a ServiceManager. All impure work (spawning, killing,
        polling ports) happens in ServiceManager or in a threaded worker; this
        class only wires input, the refresh tick, and the regions together.
        """

        CSS = """
        Screen { layout: vertical; }
        #header { height: 3; }
        #cards { height: auto; margin: 0 1; }
        ServiceCard {
            width: 1fr; height: auto; margin: 1 0 1 1; padding: 0 1;
            border: round $panel; background: $surface;
        }
        ServiceCard.selected { background: $boost; text-style: bold; }
        ServiceCard.state-running { border: round green; }
        ServiceCard.state-starting { border: round yellow; }
        ServiceCard.state-stopped { border: round grey; }
        ServiceCard.state-broken { border: round red; }
        ServiceCard.state-port_in_use { border: round magenta; }
        #logpanel { height: 1fr; margin: 0 1; }
        #logview { border: round $panel; background: $surface; padding: 0 1; }
        #logfilter { display: none; border-title-color: cyan; }
        #logfilter.visible { display: block; }
        #btnrow { height: 1; margin: 0 1; }
        ActionButton { width: auto; padding: 0 1; }
        #footer { height: 3; margin: 0 1; }
        """

        # The dashboard is keyboard-driven from the app level; letting
        # textual's default AUTO_FOCUS grab the log filter Input on mount
        # would silently swallow every binding into a text field.
        AUTO_FOCUS = None

        BINDINGS = [
            Binding("up", "select_prev", show=False),
            Binding("down", "select_next", show=False),
            Binding("left", "button_prev", show=False),
            Binding("right", "button_next", show=False),
            Binding("enter", "activate", show=False),
            Binding("a", "toggle_merged", show=False),
            Binding("t", "toggle_timestamps", show=False),
            Binding("slash", "focus_filter", show=False),
            Binding("escape", "clear_filter", show=False),
            Binding("c", "show_conn", show=False),
            Binding("S", "save_merged", show=False),
            Binding("question_mark", "show_help", show=False),
            Binding("q", "quit_app", show=False),
            Binding("ctrl+c", "interrupt", show=False),
            # Textual's base App ships a built-in, always-live
            # Binding("ctrl+q", "quit", priority=True) that otherwise exits
            # with zero confirmation; priority=True lets this one win so the
            # three-way stop/detach/stay question can't be bypassed.
            Binding("ctrl+q", "quit_app", show=False, priority=True),
        ]

        def __init__(
            self,
            config: Config,
            manager,
            *,
            start_all_on_entry: bool = False,
            clock=time.time,
        ) -> None:
            super().__init__()
            self.config = config
            self.manager = manager
            self.start_all_on_entry = start_all_on_entry
            self.clock = clock
            self.ui = UIState()
            self.detached = False
            self.exit_note = None
            self._busy = False
            self._shutdown_done = False
            self._tick_count = 0
            self._refresh_in_flight = False
            self._old_signal_handlers = []
            self._pending_bell = False
            self._buffered_alert_message = None
            self._mounted = False
            self._last_seq = -1
            self._log_placeholder = False
            self._spinner = itertools.cycle(SPINNER_FRAMES)
            self._spark = {name: collections.deque(maxlen=SPARK_CAPACITY) for name in SERVICE_ORDER}
            self._spark_last_seq = {name: -1 for name in SERVICE_ORDER}

        # -- composition & mount ----------------------------------------------
        def compose(self) -> ComposeResult:
            yield Static(id="header")
            with Horizontal(id="cards"):
                for name in SERVICE_ORDER:
                    yield ServiceCard(name, id="card-%s" % name)
            with Vertical(id="logpanel"):
                yield RichLog(id="logview", wrap=False, max_lines=3 * LOG_CAPACITY, auto_scroll=True)
                yield Input(id="logfilter", placeholder="filter logs… (Esc clears, Enter keeps)")
            with Horizontal(id="btnrow"):
                for i, btn in enumerate(BUTTONS):
                    yield ActionButton(i, btn)
            yield Static(id="footer")

        def on_mount(self) -> None:
            # RichLog is focusable by default; if it ever holds focus it
            # would consume ↑↓ and break the keyboard contract.
            self.query_one("#logview", RichLog).can_focus = False
            self._install_os_signal_handlers()
            threading.Thread(target=self._probe_versions, daemon=True).start()
            self.set_interval(0.1, self._on_tick)
            if self.start_all_on_entry:
                msg, style = self._aggregate(self.manager.start_all())
                self.show(msg, style)
            self._mounted = True
            self._reset_log_view()
            self._refresh_widgets()

        def _probe_versions(self) -> None:
            versions = detect_versions()
            try:
                # A closed loop makes call_from_thread raise *after* it has
                # built its internal coroutine, leaking an "never awaited"
                # RuntimeWarning into stderr -- skip the call entirely once
                # the app is winding down.
                if self._loop is not None and not self._loop.is_closed():
                    self.call_from_thread(self._set_versions, versions)
            except Exception:  # noqa: BLE001 - app may already be shutting down
                pass

        def _set_versions(self, versions) -> None:
            self.ui.versions = versions

        # -- messages -----------------------------------------------------------
        def show(self, text: str, style: str) -> None:
            self.ui.message = (text, style, self.clock() + MESSAGE_TTL)

        def _safe_refresh(self) -> None:
            """Render immediately instead of waiting up to a tick (0.1 s) —
            state-changing actions must be visible the moment they run, and
            tick-timed redraws alone leave a visible input lag."""
            try:
                self._refresh_widgets()
            except NoMatches:
                pass

        @staticmethod
        def _aggregate(results) -> "tuple[str, str]":
            rank = {"grey": 0, "green": 1, "yellow": 2, "red": 3}
            style = "grey"
            for _ok, _msg, result_style in results:
                if rank.get(result_style, 0) > rank[style]:
                    style = result_style
            return " ".join(msg for _ok, msg, _style in results), style

        def _toast(self, text: str, style: str) -> None:
            severity = {"red": "error", "yellow": "warning"}.get(style, "information")
            try:
                self.notify(text, title="azctl", severity=severity, timeout=5)
            except Exception:  # noqa: BLE001 - notifications are garnish
                pass

        # -- refresh tick ---------------------------------------------------------
        def _card_text(self, view: ServiceView, spark: str) -> Text:
            selected = SERVICE_ORDER[self.ui.selected] == view.name
            state = view.state
            colour = STATE_COLOURS[state]
            text = Text()
            text.append("▸ " if selected else "  ", style="bold cyan")
            text.append(STATE_SYMBOLS[state] + " " + state + "\n", style="bold " + colour)
            text.append(view.name.capitalize(), style="bold white")
            text.append(" · port %d\n" % view.port)
            if view.pid is not None:
                text.append("pid %d" % view.pid, style="grey70")
                if view.uptime is not None:
                    text.append("  up %s" % format_uptime(view.uptime), style="grey70")
            elif view.exit_code is not None:
                text.append("exit %d" % view.exit_code, style="grey70")
            else:
                text.append("pid —", style="grey70")
            if spark:
                text.append("\n" + spark, style=colour)
            return text

        def _refresh_cards(self, views) -> None:
            for view in views:
                card = self.query_one("#card-%s" % view.name, ServiceCard)
                card.update(self._card_text(view, render_sparkline(tuple(self._spark[view.name]))))
                for cls in ("selected", "state-running", "state-starting",
                            "state-stopped", "state-broken", "state-port_in_use"):
                    card.remove_class(cls)
                card.add_class("state-" + view.state.replace(" ", "_"))
                # "Starting" cards breathe (triangle-wave opacity); textual's
                # CSS has no keyframes, so this runs off the refresh tick.
                if view.state == STARTING:
                    phase = self.clock() % 1.2 / 1.2
                    card.styles.opacity = 0.55 + 0.45 * abs(1 - 2 * phase)
                else:
                    card.styles.opacity = 1.0
                if SERVICE_ORDER[self.ui.selected] == view.name:
                    card.add_class("selected")

        def _refresh_buttons(self) -> None:
            # Scoped to #btnrow: modal screens (quit dialog, conn-string
            # copies) reuse ActionButton and must never pick up hot styling.
            for i, widget in enumerate(self.query("#btnrow ActionButton")):
                widget.update(btn_label(widget.btn, hot=(i == self.ui.button)))

        def _update_spark(self) -> None:
            """Bucket per-service log arrivals since the last refresh.

            Tracked by global sequence number rather than buffer length, so
            the graph keeps working after the 2000-line ring buffer wraps.
            """
            for name in SERVICE_ORDER:
                lines = self.manager.logs.lines(name)
                last = self._spark_last_seq[name]
                fresh = sum(1 for line in lines if line.seq > last)
                if lines:
                    self._spark_last_seq[name] = lines[-1].seq
                self._spark[name].append(min(fresh, 99))

        def _on_tick(self) -> None:
            # set_interval's callback can still be queued for one more beat
            # while the app is unwinding; ignore ticks once we're no longer
            # running instead of querying unmounted widgets.
            if not self.is_running:
                return
            if self._tick_count % 3 == 0:
                # manager.refresh() does real socket probes per configured
                # port, which can block against an unreachable --host; keep
                # it off the UI thread, one at a time (see _confirm_then_run).
                if not self._refresh_in_flight and not self._busy:
                    self._refresh_in_flight = True
                    self._refresh_worker()
            self._tick_count += 1
            try:
                self._refresh_widgets()
            except NoMatches:
                pass

        @work(thread=True)
        def _refresh_worker(self) -> None:
            try:
                transitions = self.manager.refresh()
            except Exception:  # noqa: BLE001 - never let a worker crash the app
                transitions = []
            self.call_from_thread(self._finish_refresh, transitions)

        def _finish_refresh(self, transitions) -> None:
            self._refresh_in_flight = False
            try:
                self._update_spark()
            except Exception:  # noqa: BLE001
                pass
            broken = [t for t in transitions if t.new == BROKEN]
            if not broken:
                return
            # A modal sitting on top is meant to be an isolated decision
            # point; buffer the bell/message until it is dismissed.
            if len(self.screen_stack) > 1:
                self._pending_bell = True
                for t in broken:
                    reason = self.manager.broken_reason(t.service)
                    if reason:
                        self._buffered_alert_message = (reason, "red")
                return
            self.bell()
            for t in broken:
                reason = self.manager.broken_reason(t.service)
                if reason:
                    self.show(reason, "red")
                    self._toast(reason, "red")

        def _flush_pending_alerts(self) -> None:
            if self._pending_bell:
                self._pending_bell = False
                self.bell()
            if self._buffered_alert_message is not None:
                text, style = self._buffered_alert_message
                self._buffered_alert_message = None
                self.show(text, style)
                self._toast(text, style)

        def _refresh_widgets(self) -> None:
            self.query_one("#header", Static).update(render_header(self.config, self.ui.versions))
            views = self.manager.views()
            logview = self.query_one("#logview", RichLog)
            if self.ui.combined_logs:
                logview.border_title = "logs · all services (merged)"
            else:
                logview.border_title = "logs · " + SERVICE_ORDER[self.ui.selected].capitalize()
            if self.ui.filter_text:
                logview.border_subtitle = "/%s/" % self.ui.filter_text
            else:
                logview.border_subtitle = ""
            self._refresh_cards(views)
            self._refresh_buttons()
            self._feed_logs()
            footer = self.query_one("#footer", Static)
            spinner = next(self._spinner) if self._busy else ""
            footer.update(render_footer(self.ui, self.clock(), busy_spinner=spinner))

        # -- log feeding ------------------------------------------------------
        def _log_lines(self) -> "list[LogLine]":
            if self.ui.combined_logs:
                return self.manager.logs.merged()
            return self.manager.logs.lines(SERVICE_ORDER[self.ui.selected])

        def _render_log_line(self, line: LogLine) -> Text:
            text = Text()
            if self.ui.timestamps:
                text.append(time.strftime("%H:%M:%S ", time.localtime(line.wall)), style="grey50")
            if self.ui.combined_logs:
                colour = SERVICE_COLOURS.get(line.service, "white")
                text.append("[%s] " % line.service, style="bold " + colour)
                text.append(line.text, style=colour)
            else:
                text.append(line.text)
            return text

        def _feed_logs(self, reset: bool = False) -> None:
            logview = self.query_one("#logview", RichLog)
            needle = self.ui.filter_text.lower()
            if reset:
                logview.clear()
                self._last_seq = -1
                self._log_placeholder = False
                if not self._log_lines():
                    self._log_placeholder = True
                    if self.ui.combined_logs:
                        logview.write(Text("No output from any service yet.", style="grey50"))
                    elif not self.manager.views()[self.ui.selected].ever_started:
                        logview.write(
                            Text(
                                "%s has never run. Press Enter on [Start] to launch it."
                                % SERVICE_ORDER[self.ui.selected].capitalize(),
                                style="grey50",
                            )
                        )
                    else:
                        logview.write(Text("(no output yet)", style="grey50"))
                    return
                logview.scroll_end(animate=False)
            for line in self._log_lines():
                if line.seq <= self._last_seq:
                    continue
                self._last_seq = line.seq
                if needle and needle not in line.text.lower():
                    continue
                if self._log_placeholder:
                    # First real output after a placeholder: drop the note.
                    logview.clear()
                    self._log_placeholder = False
                logview.write(self._render_log_line(line))

        def _reset_log_view(self) -> None:
            self._feed_logs(reset=True)

        # -- mouse ------------------------------------------------------------
        def on_service_card_clicked(self, event: "ServiceCard.Clicked") -> None:
            try:
                idx = SERVICE_ORDER.index(event.service)
            except ValueError:
                return
            if idx != self.ui.selected:
                self._select_to(idx)

        def on_action_button_triggered(self, event: "ActionButton.Triggered") -> None:
            self.ui.button = event.index
            self._refresh_buttons()
            self._activate(event.index)

        # -- key actions ----------------------------------------------------------
        def _select_to(self, idx: int) -> None:
            changed = idx != self.ui.selected
            self.ui.selected = idx
            if changed:
                self.show("Selected %s." % SERVICE_ORDER[idx].capitalize(), "grey")
                self._reset_log_view()
                self._safe_refresh()

        def action_select_prev(self) -> None:
            self._select_to((self.ui.selected - 1) % len(SERVICE_ORDER))

        def action_select_next(self) -> None:
            self._select_to((self.ui.selected + 1) % len(SERVICE_ORDER))

        def action_button_prev(self) -> None:
            self.ui.button = max(0, self.ui.button - 1)
            self._refresh_buttons()

        def action_button_next(self) -> None:
            self.ui.button = min(len(BUTTONS) - 1, self.ui.button + 1)
            self._refresh_buttons()

        def action_activate(self) -> None:
            self._activate(self.ui.button)

        def action_toggle_merged(self) -> None:
            self.ui.combined_logs = not self.ui.combined_logs
            self.show("Merged log view %s." % ("on" if self.ui.combined_logs else "off"), "grey")
            self._reset_log_view()
            self._safe_refresh()

        def action_toggle_timestamps(self) -> None:
            self.ui.timestamps = not self.ui.timestamps
            self.show("Timestamps %s." % ("on" if self.ui.timestamps else "off"), "grey")
            self._reset_log_view()
            self._safe_refresh()

        def action_focus_filter(self) -> None:
            flt = self.query_one("#logfilter", Input)
            flt.add_class("visible")
            flt.focus()

        def action_clear_filter(self) -> None:
            flt = self.query_one("#logfilter", Input)
            if flt.has_class("visible"):
                flt.remove_class("visible")
                if self.ui.filter_text:
                    self.ui.filter_text = ""
                    self._reset_log_view()
                    self._safe_refresh()
                self.show("Log filter cleared.", "grey")

        def on_input_changed(self, event: Input.Changed) -> None:
            if event.input.id == "logfilter":
                self.ui.filter_text = event.value.strip()
                self._reset_log_view()
                self._safe_refresh()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            if event.input.id == "logfilter":
                self.set_focus(None)

        def action_show_conn(self) -> None:
            name = SERVICE_ORDER[self.ui.selected]
            copy_osc52(connection_string(name, self.config))
            self.show(
                "Copied %s connection string to clipboard (OSC 52)." % name.capitalize(),
                "green",
            )
            self.push_screen(
                ConnScreen(self.config, self.ui.selected),
                lambda _r=None: self._flush_pending_alerts(),
            )

        def action_save_merged(self) -> None:
            _ok, msg, style = self.manager.save_merged_log()
            self.show(msg, style)
            self._toast(msg, style)

        def action_show_help(self) -> None:
            self.push_screen(HelpScreen(), lambda _r=None: self._flush_pending_alerts())

        def action_quit_app(self) -> None:
            if self._busy:
                self.show("Still finishing the previous action — try again in a moment.", "grey")
            elif self.manager.any_owned():
                self.push_screen(QuitScreen(), self._on_quit_result)
            else:
                self.exit()  # nothing running: quit quietly

        def action_interrupt(self) -> None:
            # Same three-way question as q: a stray Ctrl+C must never tear
            # down owned services silently (BEHAVIOR.md confirmation contract).
            if self._busy:
                return
            if self.manager.any_owned():
                self.push_screen(QuitScreen(), self._on_quit_result)
            else:
                self.exit()

        # -- button actions ---------------------------------------------------
        def _activate(self, idx: int) -> None:
            if self._busy:
                self.show("Still finishing the previous action…", "grey")
                return
            btn = BUTTONS[idx]
            name = SERVICE_ORDER[self.ui.selected]
            title = name.capitalize()
            if btn.action == "start":
                _ok, msg, style = self.manager.start(name)
                self.show(msg, style)
                self._toast(msg, style)
            elif btn.action == "stop":
                if not self.manager.owns_live(name):
                    _ok, msg, style = self.manager.stop(name)  # grey "not running"
                    self.show(msg, style)
                else:
                    self._confirm_then_run("Stop %s?" % title, lambda: self.manager.stop(name))
            elif btn.action == "restart":
                if not self.manager.owns_live(name):
                    self.show("%s is not running — use Start." % title, "grey")
                else:
                    self._confirm_then_run("Restart %s?" % title, lambda: self.manager.restart(name))
            elif btn.action == "save":
                _ok, msg, style = self.manager.save_service_log(name)
                self.show(msg, style)
                self._toast(msg, style)
            elif btn.action == "free_port":
                self._free_port(name)
            elif btn.action == "start_all":
                msg, style = self._aggregate(self.manager.start_all())
                self.show(msg, style)
                self._toast(msg, style)
            elif btn.action == "stop_all":
                if not self.manager.any_owned():
                    self.show("No services are running.", "grey")
                else:
                    self._confirm_then_run("Stop all services?", self._stop_all_action)

        def _stop_all_action(self) -> "tuple[bool, str, str]":
            msg, style = self._aggregate(self.manager.stop_all())
            return True, msg, style

        def _free_port(self, name: str) -> None:
            port = self.config.port_for(name)
            holder = pid_on_port(port)
            if holder is None:
                if port_open(self.config.host, port):
                    self.show(
                        "Port %d is in use but the owning process could not be identified." % port,
                        "red",
                    )
                else:
                    self.show("Nothing is listening on port %d." % port, "grey")
                return
            pid, pname = holder
            if pid is None:
                self.show(
                    "Port %d is in use but the owning process could not be identified "
                    "(try elevated privileges)." % port,
                    "red",
                )
                return
            # Snapshot which pids are our own children *before* the kill, so
            # the card can honestly flip to stopped if we shoot our own service.
            owned = {}
            for view in self.manager.views():
                if view.pid is not None:
                    owned[view.pid] = view.name

            def action():
                def on_killed(killed_pid):
                    svc = owned.get(killed_pid)
                    if svc is not None:
                        self.manager.notice_external_kill(svc)

                return free_port_flow(
                    port,
                    lambda _prompt: True,  # this modal *is* the confirmation
                    host=self.config.host,
                    on_killed=on_killed,
                )

            self._confirm_then_run("Kill %s (PID %d) on port %d?" % (pname, pid, port), action)

        # -- confirmations, dispatched on a threaded worker --------------------
        def _confirm_then_run(self, prompt: str, action) -> None:
            self.push_screen(ConfirmScreen(prompt), lambda ok: self._on_confirm(ok, action, prompt))

        def _on_confirm(self, ok: bool, action, prompt: str) -> None:
            self._flush_pending_alerts()
            if not ok:
                self.show("Cancelled.", "grey")
                return
            self._busy = True
            self.show(prompt.rstrip("?") + "…", "grey")
            self._run_confirmed(action)

        @work(thread=True)
        def _run_confirmed(self, action) -> None:
            try:
                _ok, msg, style = action()
            except Exception as exc:  # noqa: BLE001 - never let a worker crash the app
                msg, style = "Action failed: %s" % exc, "red"
            self.call_from_thread(self._finish_confirmed, msg, style)

        def _finish_confirmed(self, msg: str, style: str) -> None:
            self._busy = False
            self.show(msg, style)
            self._toast(msg, style)
            self._reset_log_view()

        def _on_quit_result(self, result: str) -> None:
            self._flush_pending_alerts()
            if result == "stop":
                self.exit_note = "Stopped all services."
                self.show("Stopping services…", "grey")
                self._do_shutdown()  # _finish_shutdown() calls self.exit() once done
            elif result == "detach":
                self.manager.detach_all()
                self.detached = True
                self.exit_note = "Left the services running — azctl no longer owns them."
                self.exit()
            # "stay": nothing to do, the screen already closed itself

        # -- shutdown, the single choke point every exit path funnels through --
        #
        # manager.shutdown() blocks for up to ~3 s per stuck service; running
        # that on the asyncio thread would freeze rendering/input. Signal
        # masking must happen on the main thread, so _do_shutdown masks here
        # and hands the blocking work to a worker thread.
        def _mask_shutdown_signals(self) -> None:
            self._old_signal_handlers = []
            for signame in ("SIGINT", "SIGTERM", "SIGHUP"):
                signum = getattr(signal, signame, None)
                if signum is None:
                    continue
                try:
                    self._old_signal_handlers.append((signum, signal.signal(signum, signal.SIG_IGN)))
                except (ValueError, OSError):
                    pass

        def _restore_shutdown_signals(self) -> None:
            for signum, prev in self._old_signal_handlers:
                try:
                    signal.signal(signum, prev)
                except (ValueError, OSError):
                    pass
            self._old_signal_handlers = []

        def _do_shutdown(self) -> None:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            self._busy = True  # blocks other actions/re-entrant quit prompts
            self._mask_shutdown_signals()
            self._shutdown_worker()

        @work(thread=True)
        def _shutdown_worker(self) -> None:
            try:
                self.manager.shutdown()
            finally:
                self.call_from_thread(self._finish_shutdown)

        def _finish_shutdown(self) -> None:
            self._restore_shutdown_signals()
            self._busy = False
            self.exit()

        def _install_os_signal_handlers(self) -> None:
            # loop.add_signal_handler runs its callback as an ordinary loop
            # callback (no thread hop needed); NotImplementedError on Windows
            # just leaves the signal unhandled by us, same as before.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            for signame in ("SIGINT", "SIGTERM", "SIGHUP"):
                signum = getattr(signal, signame, None)
                if signum is None:
                    continue
                try:
                    loop.add_signal_handler(signum, self._shutdown_from_signal)
                except (NotImplementedError, RuntimeError, ValueError, OSError):
                    pass

        def _shutdown_from_signal(self) -> None:
            self.exit_note = "Interrupted — stopped all services."
            self._do_shutdown()


def run_dashboard(config: Config, start_all_on_entry: bool = False) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "azctl's dashboard needs an interactive terminal. "
            "Try 'azctl status' for a read-only snapshot."
        )
        return 2
    manager = ServiceManager(config)
    app = AzctlApp(config, manager, start_all_on_entry=start_all_on_entry)
    try:
        app.run()
    finally:
        # Idempotent safety net: runs unconditionally (normal exit, exception,
        # or abrupt teardown) and is a no-op on the detach ("leave running")
        # path too, since ServiceManager.detach_all() already cleared every
        # svc.proc — never orphan a child.
        manager.shutdown()
    if app.exit_note:
        print(app.exit_note)
    return 0

# --- main: argparse + dispatch ------------------------------------------ §11
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="azctl",
        description="A terminal dashboard for the Azurite storage emulator.",
    )
    parser.add_argument("--host", default=None, help="bind/probe host (default 127.0.0.1)")
    parser.add_argument("--blob-port", type=int, default=None, help="Blob port (default 10000)")
    parser.add_argument("--queue-port", type=int, default=None, help="Queue port (default 10001)")
    parser.add_argument("--table-port", type=int, default=None, help="Table port (default 10002)")
    parser.add_argument("--data-dir", default=None, help="Azurite data location (default ~/.azurite)")
    parser.add_argument(
        "--version", action="version", version="azctl " + __version__,
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("up", help="open the dashboard and start all three services")
    status = sub.add_parser("status", help="one read-only snapshot, then exit")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("watch", help="self-refreshing read-only status view")
    fp = sub.add_parser("free-ports", help="find and kill whatever is holding the Azurite ports")
    fp.add_argument("--yes", action="store_true", help="skip the confirmation prompts")
    fp.add_argument("--service", choices=list(SERVICE_ORDER), help="target one service's port only")
    return parser


# Read-only/informational commands that already have a complete stdlib+psutil
# fallback path (cmd_status/_print_plain_status, cmd_watch, cmd_free_ports are
# all _HAVE_DEPS-guarded; free-ports degrades to "could not be identified"
# without psutil rather than crashing) — these must work even when rich and
# psutil haven't been bootstrapped yet, per BEHAVIOR.md's "read-only view I
# can run anywhere ... without any chance of disturbing what is running".
_NO_DEPS_COMMANDS = ("status", "watch", "free-ports")


def command_needs_deps(command) -> bool:
    """False for the read-only commands (and argparse's own --help, which
    exits before this is ever consulted); True for everything else,
    including the dashboard/`up` (both need rich)."""
    return command not in _NO_DEPS_COMMANDS


def main(argv=None) -> int:
    # Parse argv first: argparse is stdlib-only, so --help and the read-only
    # commands can be served without ever needing rich/psutil to be
    # importable. Only commands that actually need rich trigger the
    # network-touching bootstrap.
    args = build_parser().parse_args(argv)
    if not _HAVE_DEPS and command_needs_deps(args.command):
        _bootstrap_and_reexec()  # installs deps and re-execs; never returns
    file_dict = load_config(config_path(os.environ, os.path.expanduser("~"), os.name == "nt"))
    config = resolve_config(args, file_dict)
    if args.command == "status":
        return cmd_status(config, as_json=args.json)
    if args.command == "watch":
        return cmd_watch(config)
    if args.command == "free-ports":
        return cmd_free_ports(config, assume_yes=args.yes, service=args.service)
    return run_dashboard(config, start_all_on_entry=(args.command == "up"))


if __name__ == "__main__":
    sys.exit(main())
