"""Port lookup, kill, and the free-port flow — against real child listeners."""

import os
import time

import azctl
from helpers import free_port, spawn_listener


def test_pid_on_port_finds_the_listener():
    port = free_port()
    proc = spawn_listener(port)
    try:
        holder = azctl.pid_on_port(port)
        assert holder is not None
        pid, name = holder
        assert pid == proc.pid
        assert isinstance(name, str) and name
    finally:
        proc.kill()
        proc.wait()


def test_pid_on_port_none_when_nothing_listens():
    assert azctl.pid_on_port(free_port()) is None


def test_free_port_flow_kills_and_confirms_by_name_and_pid():
    port = free_port()
    proc = spawn_listener(port)
    prompts = []
    try:
        def confirm(prompt):
            prompts.append(prompt)
            return True

        ok, msg, style = azctl.free_port_flow(port, confirm, sleep=lambda s: time.sleep(0.3))
        assert ok and style == "green"
        assert "free" in msg and str(port) in msg
        assert len(prompts) == 1
        assert str(proc.pid) in prompts[0] and str(port) in prompts[0]
        assert not azctl.port_open("127.0.0.1", port)
    finally:
        proc.kill()
        proc.wait()


def test_free_port_flow_nothing_listening_asks_nothing():
    port = free_port()
    called = []
    ok, msg, style = azctl.free_port_flow(port, lambda p: called.append(p) or True)
    assert ok and style == "grey"
    assert "Nothing is listening" in msg and str(port) in msg
    assert called == []  # no confirmation for a no-op


def test_free_port_flow_declined_is_cancelled_and_harmless():
    port = free_port()
    proc = spawn_listener(port)
    try:
        ok, msg, style = azctl.free_port_flow(port, lambda p: False, sleep=lambda s: None)
        assert not ok and msg == "Cancelled." and style == "grey"
        assert azctl.port_open("127.0.0.1", port)  # untouched
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def test_free_port_flow_detects_survivor_or_respawn(monkeypatch):
    port = free_port()
    proc = spawn_listener(port)
    try:
        # Neutralise the kill so the port stays held: the flow must report
        # failure and point at the still-listening process, not claim victory.
        monkeypatch.setattr(azctl, "kill_pid", lambda pid, timeout=2.0: True)
        ok, msg, style = azctl.free_port_flow(port, lambda p: True, sleep=lambda s: None)
        assert not ok and style == "red"
        assert "still in use" in msg
    finally:
        proc.kill()
        proc.wait()


def test_kill_pid_of_a_gone_process_is_success():
    port = free_port()
    proc = spawn_listener(port)
    proc.kill()
    proc.wait()
    assert azctl.kill_pid(proc.pid) is True


def test_kill_pid_swallows_access_denied(monkeypatch):
    """psutil raises AccessDenied (distinct from NoSuchProcess) when the
    caller lacks permission to signal/introspect a process — the realistic
    shape of Free-port aimed at a root-owned process on a shared machine.
    Only NoSuchProcess was ever caught here; an uncaught AccessDenied would
    propagate out of kill_pid() and crash the whole dashboard."""

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=True):
            raise azctl.psutil.AccessDenied(self.pid)

        def terminate(self):
            raise azctl.psutil.AccessDenied(self.pid)

        def kill(self):
            raise azctl.psutil.AccessDenied(self.pid)

    monkeypatch.setattr(azctl.psutil, "Process", lambda pid: FakeProc(pid))
    monkeypatch.setattr(azctl.psutil, "wait_procs", lambda procs, timeout: ([], procs))
    assert azctl.kill_pid(12345) is True  # never raises


def test_kill_pid_swallows_access_denied_on_process_lookup(monkeypatch):
    def raise_denied(pid):
        raise azctl.psutil.AccessDenied(pid)

    monkeypatch.setattr(azctl.psutil, "Process", raise_denied)
    assert azctl.kill_pid(12345) is True  # never raises


def test_observe_is_honest_about_ownership():
    port = free_port()
    proc = spawn_listener(port)
    try:
        cfg = azctl.Config(blob_port=port, queue_port=free_port(), table_port=free_port())
        snap = azctl.observe(cfg)
        assert snap["services"]["blob"]["state"] == "port in use"
        assert snap["services"]["blob"]["pid"] in (proc.pid, None)
        assert snap["services"]["queue"]["state"] == "stopped"
        assert snap["services"]["queue"]["pid"] is None
    finally:
        proc.kill()
        proc.wait()


def test_free_ports_cli_noop_path(capsys):
    cfg = azctl.Config(blob_port=free_port(), queue_port=free_port(), table_port=free_port())
    rc = azctl.cmd_free_ports(cfg, assume_yes=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Nothing is listening" in out


def test_free_ports_cli_kills_with_yes(capsys):
    port = free_port()
    proc = spawn_listener(port)
    try:
        cfg = azctl.Config(blob_port=port, queue_port=free_port(), table_port=free_port())
        rc = azctl.cmd_free_ports(cfg, assume_yes=True, service="blob")
        out = capsys.readouterr().out
        assert rc == 0
        assert str(port) in out
        assert not azctl.port_open("127.0.0.1", port)
    finally:
        proc.kill()
        proc.wait()


def test_pid_on_port_resolves_our_own_pid_via_bound_socket():
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        holder = azctl.pid_on_port(port)
        assert holder is not None
        assert holder[0] in (os.getpid(), None)
    finally:
        srv.close()
