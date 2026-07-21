"""Behavior-contract tests for azctl, mapped to CHECKLIST.md items.

Covered checklist items: 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16, 52, 53,
71, 72, 76, 77, 81, 82 — plus the non-TTY dashboard refusal.

Every subprocess test runs `python3 azctl.py ...` with a controlled
environment (PATH stripped by default) and fresh ephemeral ports, so
nothing here needs Azurite, a TTY, or the network. Fake services are
plain `python3 -c` TCP listeners. Tests that need real Azurite are
skipped unless `azurite-blob` is on PATH.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import azctl
from helpers import free_port, make_config, spawn_listener, wait_for

REPO = Path(__file__).resolve().parent.parent
AZCTL = str(REPO / "azctl.py")
RUN_TIMEOUT = 60


def base_env(tmp_path, path=""):
    """Minimal controlled environment: PATH stripped unless given."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": path,
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "TERM": "dumb",
        "COLUMNS": "200",
        "LINES": "50",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
    }
    if sys.platform == "win32":
        # Windows equivalents of HOME/XDG dirs, so azctl resolves the same
        # sandboxed locations the POSIX vars point at.
        env["USERPROFILE"] = str(home)
        env["APPDATA"] = str(tmp_path / "xdg-config")
        env["LOCALAPPDATA"] = str(tmp_path / "xdg-cache")
        # A Windows Python subprocess cannot even import socket without these.
        for key in (
            "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
            "TEMP", "TMP", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
        ):
            if key in os.environ:
                env[key] = os.environ[key]
    return env


def port_args(blob=None, queue=None, table=None):
    blob = blob if blob is not None else free_port()
    queue = queue if queue is not None else free_port()
    table = table if table is not None else free_port()
    return [
        "--blob-port", str(blob),
        "--queue-port", str(queue),
        "--table-port", str(table),
    ]


def run_azctl(args, tmp_path, env=None, timeout=RUN_TIMEOUT, stdin=subprocess.DEVNULL):
    return subprocess.run(
        [sys.executable, AZCTL] + args,
        env=env if env is not None else base_env(tmp_path),
        stdin=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def reap(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def assert_no_traceback(result):
    assert "Traceback" not in result.stderr, result.stderr
    assert "Traceback" not in result.stdout, result.stdout


# --- status: snapshot, --json shape, port-in-use honesty ---------------------


class TestStatus:
    def test_snapshot_prints_all_three_and_exits_zero(self, tmp_path):
        """Item 5: one snapshot of all three services, then exit."""
        result = run_azctl(port_args() + ["status"], tmp_path)
        assert result.returncode == 0
        assert_no_traceback(result)
        for name in ("Blob", "Queue", "Table"):
            assert name in result.stdout

    def test_foreign_listener_reports_port_in_use_not_running(self, tmp_path):
        """Items 7, 76: an outside observer owns nothing — a foreign process
        on the port is 'port in use', never 'running'."""
        port = free_port()
        listener = spawn_listener(port)
        try:
            result = run_azctl(port_args(blob=port) + ["status"], tmp_path)
            assert result.returncode == 0
            assert "port in use" in result.stdout
            assert "running" not in result.stdout
            # Read-only: the foreign listener is untouched (item 7).
            assert listener.poll() is None
        finally:
            reap(listener)

    def test_json_shape_and_nothing_else_on_stdout(self, tmp_path):
        """Item 81: status --json is machine-readable and stdout-pure."""
        port = free_port()
        listener = spawn_listener(port)
        try:
            result = run_azctl(port_args(blob=port) + ["status", "--json"], tmp_path)
            assert result.returncode == 0
            snap = json.loads(result.stdout)  # whole stdout must be the JSON
            assert set(snap.keys()) == {"host", "data_dir", "services"}
            assert set(snap["services"].keys()) == {"blob", "queue", "table"}
            for info in snap["services"].values():
                assert set(info.keys()) == {"port", "state", "pid"}
            assert snap["services"]["blob"]["state"] == "port in use"
            assert snap["services"]["blob"]["port"] == port
            assert snap["services"]["blob"]["pid"] == listener.pid
            for name in ("queue", "table"):
                assert snap["services"][name]["state"] == "stopped"
                assert snap["services"][name]["pid"] is None
        finally:
            reap(listener)

    def test_status_reaches_no_mutating_code(self, monkeypatch, tmp_path, capsys):
        """Items 6, 9, 77: no start/stop/kill path is reachable from the
        read-only commands. Every mutating primitive is booby-trapped; the
        status and watch snapshot paths must not detonate any of them."""
        port = free_port()
        listener = spawn_listener(port)  # spawned before Popen is trapped
        try:
            def bomb(*_args, **_kwargs):
                raise AssertionError("mutating path invoked from a read-only command")

            monkeypatch.setattr(azctl, "kill_pid", bomb)
            monkeypatch.setattr(azctl.subprocess, "Popen", bomb)
            monkeypatch.setattr(azctl.os, "kill", bomb)
            monkeypatch.setattr(azctl.ServiceManager, "start", bomb)
            monkeypatch.setattr(azctl.ServiceManager, "stop", bomb)
            config = azctl.Config(
                blob_port=port,
                queue_port=free_port(),
                table_port=free_port(),
                data_dir=str(tmp_path),
            )
            assert azctl.cmd_status(config) == 0
            assert azctl.cmd_status(config, as_json=True) == 0
            # watch's per-tick body is exactly these two renderers.
            azctl._print_plain_status(config)
            azctl._status_renderable(config)
            capsys.readouterr()
            assert listener.poll() is None
        finally:
            monkeypatch.undo()  # disarm the bombs before our own cleanup kills
            reap(listener)


# --- watch: exit message, read-only ------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="delivering SIGINT to a child needs POSIX signal semantics",
)
class TestWatch:
    def test_watch_exit_message_and_leaves_services_alone(self, tmp_path):
        """Items 8, 9, 10: watch refreshes until stopped, then states plainly
        that services were left exactly as they were."""
        port = free_port()
        listener = spawn_listener(port)
        proc = subprocess.Popen(
            [sys.executable, AZCTL] + port_args(blob=port) + ["watch"],
            env=base_env(tmp_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            # Read the entire first snapshot (header + three service rows) so
            # SIGINT lands in the inter-refresh sleep, not mid-print.
            seen = []
            while not seen or "table" not in seen[-1]:
                line = proc.stdout.readline()
                assert line, "watch exited before printing a full snapshot"
                seen.append(line)
            assert "azctl" in seen[0]
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=30)
            out = "".join(seen) + out
            assert proc.returncode == 0
            assert "Traceback" not in err
            assert "Stopped watching. Services were left exactly as they were." in out
            assert "port in use" in out  # it observed, and only observed
            assert listener.poll() is None  # the listener was left untouched
        finally:
            reap(proc)
            reap(listener)


# --- free-ports: table, confirmation, --yes, --service, exit codes -----------


class TestFreePorts:
    def test_nothing_listening_is_a_noop_exit_zero(self, tmp_path):
        """Items 11, 53: with nothing on any port, say so and do nothing."""
        result = run_azctl(port_args() + ["free-ports"], tmp_path)
        assert result.returncode == 0
        assert "Nothing is listening on port(s)" in result.stdout
        assert "Nothing to do." in result.stdout
        assert_no_traceback(result)

    def test_yes_kills_and_exits_zero(self, tmp_path):
        """Items 11, 13, 15: --yes skips the question, the table names the
        process and PID, the kill is verified before claiming success."""
        port = free_port()
        listener = spawn_listener(port)
        try:
            result = run_azctl(
                port_args(blob=port) + ["free-ports", "--yes", "--service", "blob"],
                tmp_path,
            )
            assert result.returncode == 0
            assert str(listener.pid) in result.stdout  # table names the PID
            # ... and the process (name casing varies: python/Python/python.exe)
            assert "python" in result.stdout.lower()
            assert "Killed" in result.stdout
            assert "is free" in result.stdout
            assert wait_for(lambda: listener.poll() is not None)
            assert not azctl.port_open("127.0.0.1", port)
        finally:
            reap(listener)

    def test_without_yes_declining_cancels_and_exits_one(self, tmp_path):
        """Item 12: it always asks first; declining (EOF on stdin) kills
        nothing and the exit code reports the port still held."""
        port = free_port()
        listener = spawn_listener(port)
        try:
            result = run_azctl(
                port_args(blob=port) + ["free-ports", "--service", "blob"],
                tmp_path,
            )
            assert result.returncode == 1
            assert "Cancelled." in result.stdout
            assert listener.poll() is None  # nothing was killed
            assert azctl.port_open("127.0.0.1", port)
        finally:
            reap(listener)

    def test_service_flag_targets_only_that_port(self, tmp_path):
        """Item 14: --service kills only the named service's port holder."""
        blob_port, queue_port = free_port(), free_port()
        blob_listener = spawn_listener(blob_port)
        queue_listener = spawn_listener(queue_port)
        try:
            result = run_azctl(
                port_args(blob=blob_port, queue=queue_port)
                + ["free-ports", "--yes", "--service", "blob"],
                tmp_path,
            )
            assert result.returncode == 0
            assert wait_for(lambda: blob_listener.poll() is not None)
            assert queue_listener.poll() is None  # untargeted port untouched
            assert azctl.port_open("127.0.0.1", queue_port)
        finally:
            reap(blob_listener)
            reap(queue_listener)

    def test_confirmation_names_process_pid_and_port(self):
        """Items 12, 52: the question names the actual holder before any kill,
        and declining changes nothing."""
        port = free_port()
        listener = spawn_listener(port)
        prompts = []
        try:
            def decline(prompt):
                prompts.append(prompt)
                return False

            ok, msg, style = azctl.free_port_flow(port, decline, sleep=lambda _s: None)
            assert not ok
            assert msg == "Cancelled."
            assert style == "grey"
            assert len(prompts) == 1
            assert "Kill" in prompts[0]
            assert "PID %d" % listener.pid in prompts[0]
            assert str(port) in prompts[0]
            assert listener.poll() is None
        finally:
            reap(listener)

    def test_respawned_holder_is_reported_not_claimed_free(self):
        """Items 15, 16: after the kill it waits and re-checks; a respawned
        listener is called out with the likely cause, never a false success."""
        port = free_port()
        first = spawn_listener(port)
        sleeps = []
        respawned = {}
        try:
            def sleep_then_respawn(seconds):
                sleeps.append(seconds)
                respawned["proc"] = spawn_listener(port)  # the "supervisor"

            ok, msg, style = azctl.free_port_flow(
                port, lambda _prompt: True, sleep=sleep_then_respawn
            )
            assert not ok
            assert style == "red"
            assert "still in use" in msg
            assert "restarted" in msg
            assert "Stop it at the source." in msg
            assert sleeps == [1.5]  # it genuinely waited before re-checking
            assert wait_for(lambda: first.poll() is not None)
        finally:
            reap(first)
            if "proc" in respawned:
                reap(respawned["proc"])


# --- missing azurite / missing node ------------------------------------------


class TestMissingRuntime:
    def test_start_without_azurite_is_red_message_no_crash(self, monkeypatch, tmp_path):
        """Item 71: empty PATH means no azurite; Start returns a red message
        naming the exact install command instead of crashing or hanging."""
        monkeypatch.setenv("PATH", "")
        config = make_config(azctl, tmp_path)
        manager = azctl.ServiceManager(config)
        ok, msg, style = manager.start("blob")
        assert not ok
        assert style == "red"
        assert "npm install -g azurite" in msg
        # Nothing was launched and the state machine is undisturbed.
        assert manager.views()[0].pid is None
        assert manager.refresh() == []
        assert manager.views()[0].state == azctl.STOPPED

    async def test_dashboard_start_button_shows_the_install_hint(self, monkeypatch, tmp_path):
        """Item 71, through the TUI seam: the Start button surfaces the same
        red message on the dashboard's message line (no event loop needed)."""
        monkeypatch.setenv("PATH", "")
        config = make_config(azctl, tmp_path)
        manager = azctl.ServiceManager(config)
        app = azctl.AzctlApp(config, manager, clock=lambda: 100.0)
        async with app.run_test() as pilot:
            app._activate(0)  # the Start button (BUTTONS[0])
            await pilot.pause()
            text, style, _expires = app.ui.message
            assert "npm install -g azurite" in text
            assert style == "red"
            assert app.is_running  # the dashboard did not fall over

    def test_missing_node_runtime_is_red_message_no_traceback(self, tmp_path):
        """Item 72: a missing Node runtime is a clear red message too."""
        def no_node(*_args, **_kwargs):
            raise FileNotFoundError("node")

        config = make_config(azctl, tmp_path)
        manager = azctl.ServiceManager(
            config,
            command_for=lambda name, cfg: ["azurite-" + name],
            spawner=no_node,
        )
        ok, msg, style = manager.start("blob")
        assert not ok
        assert style == "red"
        assert "Node" in msg
        assert "npm install -g azurite" in msg


# --- config file vs CLI flags ------------------------------------------------


class TestConfig:
    def write_config(self, tmp_path, content):
        cfg_dir = tmp_path / "xdg-config" / "azctl"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text(content, encoding="utf-8")

    def test_config_file_overrides_defaults(self, tmp_path):
        """Item 82: file overrides defaults."""
        blob_port, queue_port = free_port(), free_port()
        self.write_config(
            tmp_path,
            json.dumps({"blob_port": blob_port, "queue_port": queue_port}),
        )
        result = run_azctl(["status", "--json"], tmp_path)
        assert result.returncode == 0
        snap = json.loads(result.stdout)
        assert snap["services"]["blob"]["port"] == blob_port
        assert snap["services"]["queue"]["port"] == queue_port
        assert snap["services"]["table"]["port"] == 10002  # untouched default

    def test_cli_flags_override_config_file(self, tmp_path):
        """Item 82: flags override file; file still applies to unflagged keys."""
        file_blob, file_queue, flag_blob = free_port(), free_port(), free_port()
        self.write_config(
            tmp_path,
            json.dumps({"blob_port": file_blob, "queue_port": file_queue}),
        )
        result = run_azctl(
            ["--blob-port", str(flag_blob), "status", "--json"], tmp_path
        )
        assert result.returncode == 0
        snap = json.loads(result.stdout)
        assert snap["services"]["blob"]["port"] == flag_blob  # flag wins
        assert snap["services"]["queue"]["port"] == file_queue  # file survives

    def test_invalid_config_warns_and_falls_back(self, tmp_path):
        """Item 82: a broken config file warns and falls back to defaults —
        never a crash."""
        self.write_config(tmp_path, "{this is not json")
        result = run_azctl(["status", "--json"], tmp_path)
        assert result.returncode == 0
        assert_no_traceback(result)
        assert "warning" in result.stderr
        snap = json.loads(result.stdout)
        assert snap["services"]["blob"]["port"] == 10000  # defaults in force


# --- dashboard politely refuses a non-TTY ------------------------------------


class TestNonTTY:
    def test_dashboard_refuses_without_a_terminal(self, tmp_path):
        result = run_azctl(port_args(), tmp_path)
        assert result.returncode == 2
        assert "interactive terminal" in result.stdout
        assert "azctl status" in result.stdout  # points at the safe alternative
        assert_no_traceback(result)

    def test_up_refuses_without_a_terminal_and_starts_nothing(self, tmp_path):
        blob_port = free_port()
        result = run_azctl(port_args(blob=blob_port) + ["up"], tmp_path)
        assert result.returncode == 2
        assert "interactive terminal" in result.stdout
        assert not azctl.port_open("127.0.0.1", blob_port)  # nothing launched


# --- real azurite (skipped unless installed) ---------------------------------


@pytest.mark.skipif(
    shutil.which("azurite-blob") is None, reason="azurite is not installed"
)
class TestRealAzurite:
    def test_start_reaches_running_then_stop_frees_the_port(self, tmp_path):
        config = make_config(azctl, tmp_path)
        manager = azctl.ServiceManager(config)
        try:
            ok, msg, style = manager.start("blob")
            assert ok, msg
            assert style == "green"
            assert wait_for(
                lambda: azctl.port_open(config.host, config.blob_port), timeout=30
            ), "real azurite-blob never answered on its port"
            manager.refresh()
            view = manager.views()[0]
            assert view.state == azctl.RUNNING
            assert view.pid is not None
        finally:
            manager.shutdown()
        deadline = time.time() + 10
        while azctl.port_open(config.host, config.blob_port) and time.time() < deadline:
            time.sleep(0.1)
        assert not azctl.port_open(config.host, config.blob_port)
        assert manager.views()[0].state == azctl.STOPPED
