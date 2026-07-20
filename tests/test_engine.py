"""ServiceManager engine tests — no Azurite, fake commands and fake clocks."""

import socket
import sys

import azctl
from helpers import DIER_SCRIPT, SLEEPER_SCRIPT, listener_command, make_config, wait_for


def view_of(mgr, name):
    return {v.name: v for v in mgr.views()}[name]


def test_start_reaches_running_then_stop_reaches_stopped(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=listener_command, health_timeout=0.1)
    ok, msg, style = mgr.start("blob")
    assert ok and style == "green"
    assert "Started Blob" in msg and "PID" in msg
    try:
        assert wait_for(lambda: (mgr.refresh(), view_of(mgr, "blob").state)[1] == azctl.RUNNING)
        view = view_of(mgr, "blob")
        assert view.pid is not None
        assert view.uptime is not None and view.uptime >= 0
    finally:
        ok, msg, style = mgr.stop("blob")
    assert ok and style == "green" and msg == "Stopped Blob."
    mgr.refresh()
    assert view_of(mgr, "blob").state == azctl.STOPPED
    assert not azctl.port_open(cfg.host, cfg.blob_port)


def test_start_twice_is_a_yellow_noop(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=listener_command, health_timeout=0.1)
    mgr.start("blob")
    try:
        ok, msg, style = mgr.start("blob")
        assert not ok and style == "yellow" and "already" in msg
    finally:
        mgr.shutdown()


def test_never_answers_flips_to_broken_after_deadline(tmp_path):
    cfg = make_config(azctl, tmp_path)
    now = [0.0]
    mgr = azctl.ServiceManager(
        cfg,
        command_for=lambda name, config: [sys.executable, "-c", SLEEPER_SCRIPT],
        clock=lambda: now[0],
        health_timeout=0.05,
    )
    mgr.start("queue")
    try:
        mgr.refresh()
        assert view_of(mgr, "queue").state == azctl.STARTING
        now[0] += azctl.START_DEADLINE + 0.1
        transitions = mgr.refresh()
        assert view_of(mgr, "queue").state == azctl.BROKEN
        assert any(t.service == "queue" and t.new == azctl.BROKEN for t in transitions)
    finally:
        mgr.shutdown()


def test_dies_after_launch_is_sticky_broken_until_stop(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(
        cfg,
        command_for=lambda name, config: [sys.executable, "-c", DIER_SCRIPT],
        health_timeout=0.05,
    )
    mgr.start("table")
    proc = mgr._svcs["table"].proc
    proc.wait(timeout=10)
    mgr.refresh()
    assert view_of(mgr, "table").state == azctl.BROKEN
    assert view_of(mgr, "table").exit_code == 3
    mgr.refresh()
    assert view_of(mgr, "table").state == azctl.BROKEN  # sticky, no user action
    ok, msg, style = mgr.stop("table")
    assert ok and style == "grey" and "not running" in msg
    mgr.refresh()
    assert view_of(mgr, "table").state == azctl.STOPPED


def test_missing_azurite_names_the_install_command(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=lambda name, config: None)
    ok, msg, style = mgr.start("blob")
    assert not ok and style == "red"
    assert "npm install -g azurite" in msg


def test_missing_node_runtime_is_a_clear_message(tmp_path):
    cfg = make_config(azctl, tmp_path)

    def exploding_spawner(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    mgr = azctl.ServiceManager(
        cfg,
        command_for=lambda name, config: ["definitely-not-a-real-exe"],
        spawner=exploding_spawner,
    )
    ok, msg, style = mgr.start("blob")
    assert not ok and style == "red" and "Node" in msg


def test_external_listener_is_port_in_use_and_start_refuses(tmp_path):
    cfg = make_config(azctl, tmp_path)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", cfg.blob_port))
    srv.listen(1)
    try:
        mgr = azctl.ServiceManager(cfg, command_for=listener_command, health_timeout=0.1)
        mgr.refresh()
        assert view_of(mgr, "blob").state == azctl.PORT_IN_USE
        ok, msg, style = mgr.start("blob")
        assert not ok and style == "red" and "Free port" in msg
    finally:
        srv.close()


def test_notice_external_kill_shows_stopped_not_broken(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=listener_command, health_timeout=0.1)
    mgr.start("blob")
    try:
        assert wait_for(lambda: (mgr.refresh(), view_of(mgr, "blob").state)[1] == azctl.RUNNING)
        pid = view_of(mgr, "blob").pid
        azctl.kill_pid(pid)
        mgr.notice_external_kill("blob")
        assert wait_for(lambda: (mgr.refresh(), view_of(mgr, "blob").state)[1] == azctl.STOPPED)
    finally:
        mgr.shutdown()


def test_logs_are_captured_and_saved(tmp_path):
    cfg = make_config(azctl, tmp_path)
    script = "print('hello from fake'); import time; time.sleep(0.5)"
    mgr = azctl.ServiceManager(
        cfg,
        command_for=lambda name, config: [sys.executable, "-u", "-c", script],
    )
    mgr.start("blob")
    try:
        assert wait_for(lambda: any("hello from fake" in ln.text for ln in mgr.logs.lines("blob")))
    finally:
        mgr.shutdown()
    ok, msg, style = mgr.save_service_log("blob", str(tmp_path))
    assert ok and style == "green"
    assert str(tmp_path) in msg and "1 lines" in msg
    assert "hello from fake" in (tmp_path / "azurite-blob.log").read_text()
    ok, msg, style = mgr.save_merged_log(str(tmp_path))
    assert ok
    assert "[blob] hello from fake" in (tmp_path / "azurite-all.log").read_text()


def test_save_empty_log_still_writes_the_file(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=lambda name, config: None)
    ok, msg, style = mgr.save_service_log("queue", str(tmp_path))
    assert ok and style == "yellow" and "empty" in msg
    assert (tmp_path / "azurite-queue.log").exists()


def test_merged_log_is_in_arrival_order():
    store = azctl.LogStore(capacity=10)
    store.append("blob", "one")
    store.append("queue", "two")
    store.append("blob", "three")
    merged = store.merged()
    assert [line.text for line in merged] == ["one", "two", "three"]
    seqs = [line.seq for line in merged]
    assert seqs == sorted(seqs)
    assert [line.text for line in store.lines("blob")] == ["one", "three"]
