#!/usr/bin/env python3
"""azctl — a single-file terminal dashboard for the Azurite storage emulator.

Behavioral contract: BEHAVIOR.md in this repository.

The file is one self-bootstrapping application: if its two dependencies
(rich, psutil) are not importable, it creates a private venv under the user
cache directory, installs them there, and re-execs itself with that venv's
python. Only the standard library runs before that point.
"""

from __future__ import annotations

# --- stdlib imports only ------------------------------------------------ §0
import argparse
import base64
import collections
import itertools
import json
import os
import pathlib
import queue
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


def _bootstrap_explain(venv_dir, vpy, detail="") -> None:
    lines = ["azctl could not set up its dependencies (rich and psutil)."]
    if detail:
        lines += ["", detail.rstrip()]
    lines += [
        "",
        "azctl keeps them in a private virtualenv (your system Python is never touched):",
        "    " + str(venv_dir),
        "",
        "The usual causes are no network access or a missing/broken pip.",
        "You can finish the setup manually with:",
        "    " + str(vpy) + " -m pip install rich psutil",
        "or install rich and psutil into any Python and re-run azctl with that python.",
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
            "azctl already tried to install them once this run, but rich/psutil "
            "are still not importable from the private virtualenv.",
        )
        raise SystemExit(1)

    if not vpy.exists():
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

    # stderr + flush: keeps stdout clean for `status --json` and survives execve.
    print(
        "azctl: first run — installing rich and psutil into %s ..." % venv_dir,
        file=sys.stderr,
        flush=True,
    )
    try:
        proc = subprocess.run(
            [str(vpy), "-m", "pip", "install", "--quiet", "rich", "psutil"],
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
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _HAVE_DEPS = True
except ImportError:
    psutil = None
    Console = Group = Layout = Live = Panel = Table = Text = None
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
    __slots__ = ("name", "proc", "launched_at", "stopping", "state", "ever_started", "exit_code", "reader")

    def __init__(self, name: str) -> None:
        self.name = name
        self.proc = None
        self.launched_at = None
        self.stopping = False
        self.state = STOPPED
        self.ever_started = False
        self.exit_code = None
        self.reader = None


def port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def default_command_for(name: str, config: Config) -> "list[str] | None":
    """Resolve the azurite-<service> executable; None when not installed."""
    exe = shutil.which("azurite-" + name)
    if exe is None:
        return None
    port = config.port_for(name)
    return [
        exe,
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

    # -- log saving ------------------------------------------------------
    def save_service_log(self, name: str, directory=None) -> "tuple[bool, str, str]":
        directory = directory or os.getcwd()
        lines = self.logs.lines(name)
        path = os.path.abspath(os.path.join(directory, "azurite-%s.log" % name))
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(line.text + "\n")
        except OSError as exc:
            return False, "Could not write %s: %s" % (path, exc), "red"
        if lines:
            return True, "Wrote %d lines to %s" % (len(lines), path), "green"
        return True, "Wrote 0 lines to %s (log was empty)" % path, "yellow"

    def save_merged_log(self, directory=None) -> "tuple[bool, str, str]":
        directory = directory or os.getcwd()
        lines = self.logs.merged()
        path = os.path.abspath(os.path.join(directory, "azurite-all.log"))
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for line in lines:
                    stamp = time.strftime("%H:%M:%S", time.localtime(line.wall))
                    fh.write("%s [%s] %s\n" % (stamp, line.service, line.text))
        except OSError as exc:
            return False, "Could not write %s: %s" % (path, exc), "red"
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
    except psutil.NoSuchProcess:
        return True
    procs = []
    try:
        procs = root.children(recursive=True)
    except psutil.NoSuchProcess:
        pass
    procs.append(root)
    for proc in procs:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
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
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=3)
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


# --- input: events, escape/SGR parser, POSIX & Windows readers ---------- §9
@dataclass
class KeyEvent:
    key: str


@dataclass
class MouseEvent:
    x: int
    y: int
    pressed: bool


_ARROWS = {ord("A"): "up", ord("B"): "down", ord("C"): "right", ord("D"): "left"}


def _parse_sgr_mouse(body: bytes, final: int) -> "MouseEvent | None":
    """body is the bytes after '<' in ESC [ < btn ; x ; y (M|m)."""
    parts = body.split(b";")
    if len(parts) != 3:
        return None
    try:
        btn, x, y = (int(p) for p in parts)
    except ValueError:
        return None
    if btn != 0:  # only plain left-button events; wheel/drag parsed-and-dropped
        return None
    return MouseEvent(x, y, final == ord("M"))


def parse_bytes(buf: bytes) -> "tuple[list, bytes]":
    """Pure incremental key/mouse parser. Returns (events, unconsumed tail).

    The tail is non-empty only for a trailing incomplete escape sequence;
    the POSIX reader flushes a lone trailing ESC as an 'esc' key after 50 ms.
    """
    events = []
    i = 0
    n = len(buf)
    while i < n:
        byte = buf[i]
        if byte == 0x1B:
            if i + 1 >= n:
                break  # lone ESC at end: keep as remainder
            nxt = buf[i + 1]
            if nxt == ord("["):
                j = i + 2
                while j < n and not (0x40 <= buf[j] <= 0x7E):
                    j += 1
                if j >= n:
                    break  # incomplete CSI: keep as remainder
                final = buf[j]
                body = buf[i + 2 : j]
                if final in _ARROWS and not body:
                    events.append(KeyEvent(_ARROWS[final]))
                elif final in (ord("M"), ord("m")) and body.startswith(b"<"):
                    mouse = _parse_sgr_mouse(body[1:], final)
                    if mouse is not None:
                        events.append(mouse)
                # any other CSI: consumed and dropped, never leaks as junk keys
                i = j + 1
                continue
            if nxt == ord("O"):  # SS3 arrows (application cursor mode)
                if i + 2 >= n:
                    break
                if buf[i + 2] in _ARROWS:
                    events.append(KeyEvent(_ARROWS[buf[i + 2]]))
                i += 3
                continue
            events.append(KeyEvent("esc"))
            i += 1
            continue
        if byte in (0x0D, 0x0A):
            events.append(KeyEvent("enter"))
        elif byte == 0x03:
            events.append(KeyEvent("ctrl-c"))
        elif 0x20 <= byte <= 0x7E:
            events.append(KeyEvent(chr(byte)))  # case-sensitive: 'S' != 's'
        i += 1
    return events, buf[i:]


class PosixInputReader:
    """cbreak keyboard + SGR mouse reporting on a POSIX tty."""

    def __init__(self) -> None:
        self.events = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self._saved = None
        self._fd = None

    @staticmethod
    def _write_ctl(seq: str) -> None:
        try:
            sys.__stdout__.write(seq)
            sys.__stdout__.flush()
        except (OSError, ValueError, AttributeError):
            pass

    def start(self) -> None:
        import termios
        import tty

        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        self._write_ctl("\x1b[?1002h\x1b[?1006h")  # mouse presses, SGR encoding
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        import select

        pending = b""
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.05)
            except (OSError, ValueError):
                break
            if ready:
                try:
                    data = os.read(self._fd, 128)
                except OSError:
                    break
                if not data:
                    break
                pending += data
                parsed, pending = parse_bytes(pending)
                for event in parsed:
                    self.events.put(event)
            elif pending == b"\x1b":
                # ESC with no continuation for 50 ms: it was the Esc key.
                self.events.put(KeyEvent("esc"))
                pending = b""

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._write_ctl("\x1b[?1006l\x1b[?1002l")
        if self._saved is not None:
            import termios

            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except termios.error:
                pass
            self._saved = None


class WindowsInputReader:
    """msvcrt polling reader. No mouse on Windows — silently absent."""

    def __init__(self) -> None:
        self.events = queue.Queue()
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        import msvcrt

        prefix_map = {"H": "up", "P": "down", "K": "left", "M": "right"}
        while not self._stop.is_set():
            if not msvcrt.kbhit():
                time.sleep(0.02)
                continue
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                key = prefix_map.get(msvcrt.getwch())
                if key:
                    self.events.put(KeyEvent(key))
            elif ch in ("\r", "\n"):
                self.events.put(KeyEvent("enter"))
            elif ch == "\x1b":
                self.events.put(KeyEvent("esc"))
            elif ch == "\x03":
                self.events.put(KeyEvent("ctrl-c"))
            elif " " <= ch <= "~":
                self.events.put(KeyEvent(ch))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None


def make_input_reader():
    if os.name == "nt":
        return WindowsInputReader()
    return PosixInputReader()


# --- TUI: UIState, pure render functions, Dashboard loop ---------------- §10
@dataclass
class Button:
    label: str
    action: str
    danger: bool
    group: int


BUTTONS = (
    Button("Start", "start", False, 0),
    Button("Stop", "stop", True, 0),
    Button("Restart", "restart", True, 0),
    Button("Save", "save", False, 0),
    Button("Free port", "free_port", True, 0),
    Button("Start all", "start_all", False, 1),
    Button("Stop all", "stop_all", True, 1),
)

GROUP_SEPARATOR = " │ "

# Fixed layout arithmetic (1-based terminal rows) — the single source of
# truth shared by the renderer and mouse hit-testing.
HEADER_SIZE = 3
TABLE_SIZE = 5
FOOTER_SIZE = 4
TABLE_ROW_Y0 = HEADER_SIZE + 2       # first service row (heading sits above it)
BUTTON_ROW_FROM_BOTTOM = 2           # button bar row = height - 2

MSG_STYLES = {"green": "green", "red": "bold red", "yellow": "yellow", "grey": "grey50"}


@dataclass
class UIState:
    selected: int = 0
    button: int = 0
    mode: str = "normal"  # normal | confirm | quit | help | conn
    pending_prompt: str = ""
    combined_logs: bool = False
    timestamps: bool = False
    message: "tuple | None" = None  # (text, style, expires_at)
    versions: "tuple" = ("unknown", "unknown")


def button_spans() -> "list[tuple[int, int, int]]":
    """(index, x0, x1) 1-based column span of each rendered [Button] token."""
    spans = []
    x = 1
    for i, btn in enumerate(BUTTONS):
        if i == 0:
            sep = ""
        elif BUTTONS[i - 1].group != btn.group:
            sep = GROUP_SEPARATOR
        else:
            sep = " "
        x += len(sep)
        token_len = len(btn.label) + 2  # [label]
        spans.append((i, x, x + token_len - 1))
        x += token_len
    return spans


def render_header(config: Config, versions) -> "Panel":
    azurite_ver, node_ver = versions
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append("azctl", style="bold cyan")
    text.append(" · Azurite Storage Emulator", style="bold")
    text.append("   host %s · data %s" % (config.host, config.data_dir))
    text.append("   azurite %s · node %s" % (azurite_ver, node_ver), style="grey50")
    return Panel(text)


def render_table(views, selected: int) -> "Table":
    table = Table(box=None, show_header=True, header_style="bold", padding=(0, 1), pad_edge=False)
    table.add_column("Service")
    table.add_column("Status")
    table.add_column("Port", justify="right")
    table.add_column("PID", justify="right")
    table.add_column("Uptime", justify="right")
    for i, view in enumerate(views):
        marker = "▸ " if i == selected else "  "
        table.add_row(
            Text(marker + view.name.capitalize(), style="bold" if i == selected else ""),
            Text("%s %s" % (STATE_SYMBOLS[view.state], view.state), style=STATE_COLOURS[view.state]),
            str(view.port),
            str(view.pid) if view.pid is not None else "—",
            format_uptime(view.uptime) if view.uptime is not None else "—",
        )
    return table


def render_logs(lines, *, combined, timestamps, service, ever_started, height) -> "Panel":
    inner = max(1, height - 2)  # panel borders
    shown = lines[-inner:]
    text = Text(no_wrap=True, overflow="ellipsis")
    if not shown:
        if combined:
            text.append("No output from any service yet.", style="grey50")
        elif not ever_started:
            text.append(
                "%s has never run. Press Enter on [Start] to launch it." % service.capitalize(),
                style="grey50",
            )
        else:
            text.append("(no output yet)", style="grey50")
    else:
        for line in shown:
            if len(text) > 0:
                text.append("\n")
            if timestamps:
                stamp = time.strftime("%H:%M:%S ", time.localtime(line.wall))
                text.append(stamp, style="grey50")
            if combined:
                colour = SERVICE_COLOURS.get(line.service, "white")
                text.append("[%s] " % line.service, style="bold " + colour)
                text.append(line.text, style=colour)
            else:
                text.append(line.text)
    title = "logs · all services (merged)" if combined else "logs · " + service.capitalize()
    return Panel(text, title=title, title_align="left")


def _legend_text() -> "Text":
    text = Text(no_wrap=True, overflow="ellipsis")
    for state in (RUNNING, STARTING, STOPPED, BROKEN, PORT_IN_USE):
        if len(text) > 0:
            text.append("   ")
        text.append("%s %s" % (STATE_SYMBOLS[state], state), style=STATE_COLOURS[state])
    return text


def render_button_bar(active: int) -> "Text":
    bar = Text(no_wrap=True, overflow="ellipsis")
    for i, btn in enumerate(BUTTONS):
        if i == 0:
            sep = ""
        elif BUTTONS[i - 1].group != btn.group:
            sep = GROUP_SEPARATOR
        else:
            sep = " "
        bar.append(sep)
        if i == active:
            style = "bold red reverse" if btn.danger else "bold reverse"
        else:
            style = "red" if btn.danger else ""
        bar.append("[" + btn.label + "]", style=style)
    return bar


def render_footer(ui: UIState, now: float) -> "Group":
    rows = [_legend_text()]
    if ui.mode == "confirm":
        rows.append(
            Text(
                ui.pending_prompt + "  — Enter: yes · Esc or any other key: cancel",
                style="bold yellow",
                no_wrap=True,
                overflow="ellipsis",
            )
        )
    elif ui.mode == "quit":
        rows.append(
            Text(
                "Services are still running — Enter: stop them and quit · "
                "n: leave them running and quit · Esc: stay",
                style="bold yellow",
                no_wrap=True,
                overflow="ellipsis",
            )
        )
    else:
        rows.append(render_button_bar(ui.button))
    hints = Text(
        "↑↓ service · ←→ buttons · Enter run · a all-logs · t times · "
        "c conn str · S save all · ? help · q quit",
        style="grey50",
        no_wrap=True,
        overflow="ellipsis",
    )
    hints.append("   logs: ", style="grey50")
    if ui.combined_logs:
        hints.append("ALL", style="bold yellow")
    else:
        hints.append("selected", style="grey50")
    rows.append(hints)
    if ui.message is not None and ui.message[2] > now:
        rows.append(
            Text(ui.message[0], style=MSG_STYLES.get(ui.message[1], ""), no_wrap=True, overflow="ellipsis")
        )
    else:
        rows.append(Text(""))
    return Group(*rows)


HELP_ROWS = (
    ("↑ / ↓", "Select a service — the table marker and log panel follow instantly"),
    ("← / →", "Move the highlight along the button bar"),
    ("Enter", "Run the highlighted button"),
    ("a", "Toggle the merged view of all three logs (colour-tagged, arrival order)"),
    ("t", "Toggle timestamps on log lines"),
    ("c", "Show connection strings and copy the selected one (OSC 52)"),
    ("S", "Save the merged all-services log to azurite-all.log"),
    ("?", "This help overlay"),
    ("Esc", "Cancel a confirmation / close an overlay"),
    ("q", "Quit — asks what to do with services that are still running"),
    ("mouse", "Click a table row to select it; click a button to run it"),
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


def render_conn(config: Config, selected: int) -> "Panel":
    text = Text(no_wrap=True, overflow="ellipsis")
    for i, name in enumerate(SERVICE_ORDER):
        marker = "▸ " if i == selected else "  "
        text.append("%s%s\n" % (marker, name.capitalize()), style="bold " + SERVICE_COLOURS[name])
        text.append("    %s\n" % connection_string(name, config))
    text.append(
        "\nThe selected service's string was copied to the clipboard via OSC 52.\n"
        "Press any key to close.",
        style="grey50",
    )
    return Panel(text, title="connection strings", title_align="left")


def build_frame(config: Config, views, log_lines, ui: UIState, height: int, now: float) -> "Layout":
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=HEADER_SIZE),
        Layout(name="table", size=TABLE_SIZE),
        Layout(name="logs", ratio=1),
        Layout(name="footer", size=FOOTER_SIZE),
    )
    layout["header"].update(render_header(config, ui.versions))
    layout["table"].update(render_table(views, ui.selected))
    if ui.mode == "help":
        layout["logs"].update(render_help())
    elif ui.mode == "conn":
        layout["logs"].update(render_conn(config, ui.selected))
    else:
        service = SERVICE_ORDER[ui.selected]
        ever = views[ui.selected].ever_started if ui.selected < len(views) else False
        logs_height = max(3, height - HEADER_SIZE - TABLE_SIZE - FOOTER_SIZE)
        layout["logs"].update(
            render_logs(
                log_lines,
                combined=ui.combined_logs,
                timestamps=ui.timestamps,
                service=service,
                ever_started=ever,
                height=logs_height,
            )
        )
    layout["footer"].update(render_footer(ui, now))
    return layout


class Dashboard:
    """Owns UIState + a ServiceManager; handle_event() is pure UI logic and
    fully testable without a terminal. run() is the only impure part."""

    def __init__(self, config: Config, manager, *, clock=time.time, sleep=time.sleep):
        self.config = config
        self.manager = manager
        self.clock = clock
        self.sleep = sleep
        self.ui = UIState()
        self.running = True
        self.detached = False
        self.exit_note = None
        self.size = (80, 24)  # (width, height); refreshed every frame
        self._pending_action = None

    # -- messages --------------------------------------------------------
    def show(self, text: str, style: str) -> None:
        self.ui.message = (text, style, self.clock() + MESSAGE_TTL)

    @staticmethod
    def _aggregate(results) -> "tuple[str, str]":
        rank = {"grey": 0, "green": 1, "yellow": 2, "red": 3}
        style = "grey"
        for _ok, _msg, result_style in results:
            if rank.get(result_style, 0) > rank[style]:
                style = result_style
        return " ".join(msg for _ok, msg, _style in results), style

    def _ask(self, prompt: str, action) -> None:
        self.ui.mode = "confirm"
        self.ui.pending_prompt = prompt
        self._pending_action = action

    # -- event handling --------------------------------------------------
    def handle_event(self, event) -> None:
        if isinstance(event, MouseEvent):
            self._handle_mouse(event)
            return
        key = event.key
        mode = self.ui.mode
        if mode in ("help", "conn"):
            self.ui.mode = "normal"
            return
        if mode == "confirm":
            self.ui.mode = "normal"
            action = self._pending_action
            self._pending_action = None
            self.ui.pending_prompt = ""
            if key == "enter" and action is not None:
                _ok, msg, style = action()
                self.show(msg, style)
            else:
                self.show("Cancelled.", "grey")
            return
        if mode == "quit":
            if key == "enter":
                self.manager.shutdown()
                self.exit_note = "Stopped all services."
                self.running = False
            elif key == "n":
                self.manager.detach_all()
                self.detached = True
                self.exit_note = "Left the services running — azctl no longer owns them."
                self.running = False
            elif key == "esc":
                self.ui.mode = "normal"
            # every other key: deliberately ignored (three answers only)
            return
        # normal mode
        if key in ("up", "down"):
            step = -1 if key == "up" else 1
            self.ui.selected = (self.ui.selected + step) % len(SERVICE_ORDER)
            self.show("Selected %s." % SERVICE_ORDER[self.ui.selected].capitalize(), "grey")
        elif key == "left":
            self.ui.button = max(0, self.ui.button - 1)
        elif key == "right":
            self.ui.button = min(len(BUTTONS) - 1, self.ui.button + 1)
        elif key == "enter":
            self._activate(self.ui.button)
        elif key == "a":
            self.ui.combined_logs = not self.ui.combined_logs
            self.show("Merged log view %s." % ("on" if self.ui.combined_logs else "off"), "grey")
        elif key == "t":
            self.ui.timestamps = not self.ui.timestamps
            self.show("Timestamps %s." % ("on" if self.ui.timestamps else "off"), "grey")
        elif key == "c":
            name = SERVICE_ORDER[self.ui.selected]
            copy_osc52(connection_string(name, self.config))
            self.ui.mode = "conn"
            self.show(
                "Copied %s connection string to clipboard (OSC 52)." % name.capitalize(),
                "green",
            )
        elif key == "S":
            _ok, msg, style = self.manager.save_merged_log()
            self.show(msg, style)
        elif key == "?":
            self.ui.mode = "help"
        elif key == "q":
            if self.manager.any_owned():
                self.ui.mode = "quit"
            else:
                self.running = False  # nothing running: quit quietly
        elif key == "ctrl-c":
            if self.manager.any_owned():
                self.exit_note = "Interrupted — stopped all services."
            self.running = False

    def _handle_mouse(self, event: MouseEvent) -> None:
        if not event.pressed:
            return
        if self.ui.mode in ("help", "conn"):
            self.ui.mode = "normal"
            return
        if self.ui.mode != "normal":
            return  # confirmations are keyboard-only, clicks never skip them
        if TABLE_ROW_Y0 <= event.y < TABLE_ROW_Y0 + len(SERVICE_ORDER):
            self.ui.selected = event.y - TABLE_ROW_Y0
            self.show("Selected %s." % SERVICE_ORDER[self.ui.selected].capitalize(), "grey")
            return
        if event.y == self.size[1] - BUTTON_ROW_FROM_BOTTOM:
            for idx, x0, x1 in button_spans():
                if x0 <= event.x <= x1:
                    self.ui.button = idx
                    self._activate(idx)
                    return

    # -- button actions --------------------------------------------------
    def _activate(self, idx: int) -> None:
        btn = BUTTONS[idx]
        name = SERVICE_ORDER[self.ui.selected]
        title = name.capitalize()
        if btn.action == "start":
            _ok, msg, style = self.manager.start(name)
            self.show(msg, style)
        elif btn.action == "stop":
            if not self.manager.owns_live(name):
                _ok, msg, style = self.manager.stop(name)  # grey "not running"
                self.show(msg, style)
            else:
                self._ask("Stop %s?" % title, lambda: self.manager.stop(name))
        elif btn.action == "restart":
            if not self.manager.owns_live(name):
                self.show("%s is not running — use Start." % title, "grey")
            else:
                self._ask("Restart %s?" % title, lambda: self.manager.restart(name))
        elif btn.action == "save":
            _ok, msg, style = self.manager.save_service_log(name)
            self.show(msg, style)
        elif btn.action == "free_port":
            self._free_port(name)
        elif btn.action == "start_all":
            msg, style = self._aggregate(self.manager.start_all())
            self.show(msg, style)
        elif btn.action == "stop_all":
            if not self.manager.any_owned():
                self.show("No services are running.", "grey")
            else:
                self._ask("Stop all services?", self._stop_all_action)

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
        # Snapshot which pids are our own children *before* the kill, so the
        # row can honestly flip to "stopped" if we shoot our own service.
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
                sleep=self.sleep,
                on_killed=on_killed,
            )

        self._ask("Kill %s (PID %d) on port %d?" % (pname, pid, port), action)

    # -- the live loop (the only impure part) ----------------------------
    def run(self, start_all_on_entry: bool = False) -> int:
        console = Console()
        reader = make_input_reader()

        def probe_versions():
            self.ui.versions = detect_versions()

        threading.Thread(target=probe_versions, daemon=True).start()

        def _sig_handler(_signum, _frame):
            raise KeyboardInterrupt

        old_handlers = []
        for signame in ("SIGINT", "SIGTERM"):
            signum = getattr(signal, signame, None)
            if signum is None:
                continue
            try:
                old_handlers.append((signum, signal.signal(signum, _sig_handler)))
            except (ValueError, OSError):
                pass
        try:
            reader.start()
            if start_all_on_entry:
                msg, style = self._aggregate(self.manager.start_all())
                self.show(msg, style)
            with Live(console=console, screen=True, auto_refresh=False) as live:
                tick = 0
                while self.running:
                    while self.running:
                        try:
                            event = reader.events.get_nowait()
                        except queue.Empty:
                            break
                        self.handle_event(event)
                    if not self.running:
                        break
                    if tick % 3 == 0:
                        transitions = self.manager.refresh()
                        if any(t.new == BROKEN for t in transitions):
                            try:
                                console.file.write("\a")
                                console.file.flush()
                            except (OSError, ValueError):
                                pass
                    width, height = console.size
                    self.size = (width, height)
                    if self.ui.combined_logs:
                        lines = self.manager.logs.merged()
                    else:
                        lines = self.manager.logs.lines(SERVICE_ORDER[self.ui.selected])
                    live.update(
                        build_frame(self.config, self.manager.views(), lines, self.ui, height, self.clock()),
                        refresh=True,
                    )
                    time.sleep(0.1)
                    tick += 1
        except KeyboardInterrupt:
            if self.manager.any_owned():
                self.exit_note = "Interrupted — stopped all services."
        finally:
            reader.stop()
            if not self.detached:
                self.manager.shutdown()
            for signum, old in old_handlers:
                try:
                    signal.signal(signum, old)
                except (ValueError, OSError):
                    pass
        if self.exit_note:
            print(self.exit_note)
        return 0


def run_dashboard(config: Config, start_all_on_entry: bool = False) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "azctl's dashboard needs an interactive terminal. "
            "Try 'azctl status' for a read-only snapshot."
        )
        return 2
    manager = ServiceManager(config)
    dashboard = Dashboard(config, manager)
    return dashboard.run(start_all_on_entry=start_all_on_entry)


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
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("up", help="open the dashboard and start all three services")
    status = sub.add_parser("status", help="one read-only snapshot, then exit")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("watch", help="self-refreshing read-only status view")
    fp = sub.add_parser("free-ports", help="find and kill whatever is holding the Azurite ports")
    fp.add_argument("--yes", action="store_true", help="skip the confirmation prompts")
    fp.add_argument("--service", choices=list(SERVICE_ORDER), help="target one service's port only")
    return parser


def main(argv=None) -> int:
    if not _HAVE_DEPS:
        _bootstrap_and_reexec()  # installs deps and re-execs; never returns
    args = build_parser().parse_args(argv)
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
