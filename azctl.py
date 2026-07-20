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
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text

    _HAVE_DEPS = True
except ImportError:
    psutil = None
    Console = Live = Table = Text = None
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


# ===== TUI ============================================================== §9-10
# The interactive dashboard lives here: input readers (termios/tty+select on
# POSIX, msvcrt on Windows), the SGR mouse parser, UIState, the pure render
# functions, and the rich Live loop with buttons/confirmations/help overlay.
# Everything above this banner is engine + CLI and fully functional without it.


def run_dashboard(config: Config, start_all_on_entry: bool = False) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "azctl's dashboard needs an interactive terminal. "
            "Try 'azctl status' for a read-only snapshot."
        )
        return 2
    raise SystemExit("dashboard not yet implemented")


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
