"""Engine tests for azctl: five-state machine, lifecycle, ports, logs, config.

Everything runs without Azurite, without a TTY, and without the interactive
event loop. Fake services are plain ``python3 -c`` TCP listeners injected
through ServiceManager's ``command_for`` seam; time is driven through the
injectable ``clock`` seam so the 10-second starting->broken deadline never
costs 10 real seconds.
"""

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time

import pytest

import azctl
from helpers import (
    DIER_SCRIPT,
    SLEEPER_SCRIPT,
    free_port,
    listener_command,
    make_config,
    spawn_listener,
    wait_for,
)

RESPAWNER_SCRIPT = """\
import subprocess, sys

port = sys.argv[1]
child = (
    "import socket, sys, time\\n"
    "s = socket.socket()\\n"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\\n"
    "s.bind(('127.0.0.1', int(sys.argv[1])))\\n"
    "s.listen(50)\\n"
    "time.sleep(120)\\n"
)
while True:
    p = subprocess.Popen([sys.executable, "-c", child, port])
    p.wait()
"""


def view_of(mgr, name):
    return {v.name: v for v in mgr.views()}[name]


def refreshed_state(mgr, name):
    mgr.refresh()
    return view_of(mgr, name).state


def foreign_listener(port):
    """A listener socket owned by the test process itself (a 'foreign' squatter
    from the manager's point of view)."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(50)
    return srv


def no_sleep(_seconds):
    return None


# --- five-state machine -------------------------------------------------


def test_fresh_manager_is_stopped_everywhere(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=listener_command, health_timeout=0.05)
    mgr.refresh()
    for view in mgr.views():
        assert view.state == azctl.STOPPED
        assert view.pid is None
        assert view.uptime is None
        assert not view.ever_started
        assert view.exit_code is None


def test_start_reaches_running_then_stop_reaches_stopped(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=listener_command, health_timeout=0.1)
    ok, msg, style = mgr.start("blob")
    assert ok and style == "green"
    assert "Started Blob" in msg and "PID" in msg
    try:
        assert wait_for(lambda: refreshed_state(mgr, "blob") == azctl.RUNNING)
        view = view_of(mgr, "blob")
        assert view.pid is not None and view.ever_started
        assert view.uptime is not None and view.uptime >= 0
    finally:
        ok, msg, style = mgr.stop("blob")
    assert ok and style == "green" and msg == "Stopped Blob."
    assert refreshed_state(mgr, "blob") == azctl.STOPPED
    assert not azctl.port_open(cfg.host, cfg.blob_port)


def test_starting_holds_until_deadline_then_broken(tmp_path):
    """The 10 s grace period, via the injectable clock — no real waiting."""
    cfg = make_config(azctl, tmp_path)
    now = [1000.0]

    def clock():
        return now[0]

    mgr = azctl.ServiceManager(
        cfg,
        command_for=lambda name, config: [sys.executable, "-c", SLEEPER_SCRIPT],
        clock=clock,
        health_timeout=0.05,
    )
    started = time.monotonic()
    mgr.start("queue")
    try:
        assert view_of(mgr, "queue").state == azctl.STARTING
        assert refreshed_state(mgr, "queue") == azctl.STARTING
        now[0] += azctl.START_DEADLINE - 0.1  # 9.9 s in: still a slow starter
        assert refreshed_state(mgr, "queue") == azctl.STARTING
        now[0] += 0.2  # past 10 s: no infinite spinner
        transitions = mgr.refresh()
        assert view_of(mgr, "queue").state == azctl.BROKEN
        assert any(
            t.service == "queue" and t.old == azctl.STARTING and t.new == azctl.BROKEN
            for t in transitions
        )
    finally:
        mgr.shutdown()
    assert time.monotonic() - started < 2.0, "deadline test must not wait real time"


def test_start_twice_is_a_yellow_noop(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=listener_command, health_timeout=0.1)
    mgr.start("blob")
    try:
        ok, msg, style = mgr.start("blob")
        assert not ok and style == "yellow" and "already" in msg
    finally:
        mgr.shutdown()


def test_child_death_is_detected_and_sticky_broken(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(
        cfg,
        command_for=lambda name, config: [sys.executable, "-c", DIER_SCRIPT],
        health_timeout=0.05,
    )
    mgr.start("table")
    mgr._svcs["table"].proc.wait(timeout=10)
    assert refreshed_state(mgr, "table") == azctl.BROKEN
    assert view_of(mgr, "table").exit_code == 3
    # Sticky: still broken on the next refresh, with no user action.
    assert refreshed_state(mgr, "table") == azctl.BROKEN
    # Stop clears the sticky record and recomputes honestly.
    ok, msg, style = mgr.stop("table")
    assert ok and style == "grey" and "not running" in msg
    assert refreshed_state(mgr, "table") == azctl.STOPPED


def test_foreign_listener_is_port_in_use_never_running(tmp_path):
    """The distinction that costs developers twenty minutes: a port held by
    someone else must read 'port in use', while our own child reads 'running'."""
    cfg = make_config(azctl, tmp_path)
    srv = foreign_listener(cfg.blob_port)
    try:
        mgr = azctl.ServiceManager(
            cfg, command_for=listener_command, health_timeout=0.1
        )
        assert refreshed_state(mgr, "blob") == azctl.PORT_IN_USE
        # Same manager, own child on the queue port: RUNNING, not PORT_IN_USE.
        mgr.start("queue")
        try:
            assert wait_for(lambda: refreshed_state(mgr, "queue") == azctl.RUNNING)
            assert view_of(mgr, "blob").state == azctl.PORT_IN_USE
        finally:
            mgr.shutdown()
        # Start refuses to fight the squatter and points at Free port.
        ok, msg, style = mgr.start("blob")
        assert not ok and style == "red" and "Free port" in msg
    finally:
        srv.close()


def test_observer_reports_port_in_use_for_a_service_it_does_not_own(tmp_path):
    """BEHAVIOR.md: status is a read-only observer; it may only say 'port in
    use', never 'running', even when the port holder is a healthy service."""
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=listener_command, health_timeout=0.1)
    mgr.start("blob")
    try:
        assert wait_for(lambda: refreshed_state(mgr, "blob") == azctl.RUNNING)
        child_pid = view_of(mgr, "blob").pid
        snap = azctl.observe(cfg)
        assert snap["services"]["blob"]["state"] == azctl.PORT_IN_USE
        assert snap["services"]["blob"]["pid"] == child_pid
        assert snap["services"]["queue"]["state"] == azctl.STOPPED
        assert snap["services"]["table"]["state"] == azctl.STOPPED
    finally:
        mgr.shutdown()


def test_missing_azurite_names_the_install_command(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=lambda name, config: None)
    ok, msg, style = mgr.start("blob")
    assert not ok and style == "red"
    assert "npm install -g azurite" in msg


def test_missing_node_runtime_is_a_clear_red_message(tmp_path):
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


def test_node_missing_exit_127_is_reported_not_a_silent_broken(tmp_path):
    """The realistic 'Node missing' shape: a real npm-installed azurite-*
    binary is a `#!/usr/bin/env node` shebang script. The kernel execs
    `/usr/bin/env` fine (Popen never raises FileNotFoundError) and only
    `env` itself fails to resolve `node`, so the child exits 127 within
    milliseconds. This must not be silently swallowed into a generic,
    unexplained BROKEN with no red message anywhere (BEHAVIOR.md: 'the
    supporting runtime is missing ... a clear red message')."""
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(
        cfg,
        command_for=lambda name, config: [sys.executable, "-c", "import sys; sys.exit(127)"],
        health_timeout=0.05,
    )
    mgr.start("blob")
    mgr._svcs["blob"].proc.wait(timeout=10)
    assert refreshed_state(mgr, "blob") == azctl.BROKEN
    assert view_of(mgr, "blob").exit_code == 127
    reason = mgr.broken_reason("blob")
    assert reason is not None
    assert "Node" in reason and "npm install -g azurite" in reason
    # Sticky like the rest of the BROKEN-death record.
    assert refreshed_state(mgr, "blob") == azctl.BROKEN
    assert mgr.broken_reason("blob") == reason
    # Stop clears it, same as it clears exit_code/launched_at.
    mgr.stop("blob")
    assert mgr.broken_reason("blob") is None


def test_death_with_a_non_127_exit_code_has_no_broken_reason(tmp_path):
    """A generic crash (any other exit code) stays a plain, unexplained
    BROKEN — broken_reason is specifically for the Node-missing shape, not a
    catch-all message for every death."""
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(
        cfg,
        command_for=lambda name, config: [sys.executable, "-c", DIER_SCRIPT],
        health_timeout=0.05,
    )
    mgr.start("table")
    mgr._svcs["table"].proc.wait(timeout=10)
    assert refreshed_state(mgr, "table") == azctl.BROKEN
    assert view_of(mgr, "table").exit_code == 3
    assert mgr.broken_reason("table") is None


# --- lifecycle: restart, uptime ------------------------------------------


def test_restart_stops_then_starts_with_a_new_pid(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=listener_command, health_timeout=0.1)
    mgr.start("blob")
    try:
        assert wait_for(lambda: refreshed_state(mgr, "blob") == azctl.RUNNING)
        old_pid = view_of(mgr, "blob").pid
        ok, msg, style = mgr.restart("blob")
        assert ok and style == "green" and "Started Blob" in msg
        new_pid = view_of(mgr, "blob").pid
        assert new_pid is not None and new_pid != old_pid
        assert wait_for(lambda: refreshed_state(mgr, "blob") == azctl.RUNNING)
    finally:
        mgr.shutdown()
    assert not azctl.port_open(cfg.host, cfg.blob_port)


def test_restart_of_a_stopped_service_just_starts_it(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=listener_command, health_timeout=0.1)
    try:
        ok, msg, style = mgr.restart("queue")
        assert ok and style == "green" and "Started Queue" in msg
    finally:
        mgr.shutdown()


def test_uptime_follows_the_injected_clock(tmp_path):
    cfg = make_config(azctl, tmp_path)
    now = [100.0]

    def clock():
        return now[0]

    mgr = azctl.ServiceManager(
        cfg, command_for=listener_command, clock=clock, health_timeout=0.1
    )
    mgr.start("blob")
    try:
        assert wait_for(lambda: refreshed_state(mgr, "blob") == azctl.RUNNING)
        now[0] += 3661.0
        view = view_of(mgr, "blob")
        assert view.uptime == pytest.approx(3661.0)
        assert azctl.format_uptime(view.uptime) == "1:01:01"
    finally:
        mgr.shutdown()


def test_format_uptime_is_h_mm_ss():
    assert azctl.format_uptime(0) == "0:00:00"
    assert azctl.format_uptime(59.9) == "0:00:59"
    assert azctl.format_uptime(61) == "0:01:01"
    assert azctl.format_uptime(3600) == "1:00:00"
    assert azctl.format_uptime(3661) == "1:01:01"
    assert azctl.format_uptime(36 * 3600) == "36:00:00"  # hours are not capped
    assert azctl.format_uptime(359999) == "99:59:59"


# --- log store: ring buffer + arrival order ------------------------------


def test_ring_buffer_caps_per_service_and_drops_oldest():
    store = azctl.LogStore(capacity=5)
    for i in range(8):
        store.append("blob", "line-%d" % i)
    texts = [line.text for line in store.lines("blob")]
    assert texts == ["line-3", "line-4", "line-5", "line-6", "line-7"]


def test_merged_buffer_caps_at_three_times_capacity():
    store = azctl.LogStore(capacity=2)
    for i in range(9):
        store.append(azctl.SERVICE_ORDER[i % 3], "m-%d" % i)
    merged = [line.text for line in store.merged()]
    assert merged == ["m-3", "m-4", "m-5", "m-6", "m-7", "m-8"]  # 3 * 2 newest


def test_merged_is_global_arrival_order_not_grouped():
    store = azctl.LogStore(capacity=10)
    arrivals = [
        ("blob", "b1"),
        ("queue", "q1"),
        ("table", "t1"),
        ("blob", "b2"),
        ("queue", "q2"),
        ("blob", "b3"),
    ]
    for service, text in arrivals:
        store.append(service, text)
    merged = store.merged()
    assert [(line.service, line.text) for line in merged] == arrivals
    seqs = [line.seq for line in merged]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert [line.text for line in store.lines("blob")] == ["b1", "b2", "b3"]


def test_manager_log_capacity_seam_reaches_the_store(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(
        cfg, command_for=lambda name, config: None, log_capacity=3
    )
    for i in range(6):
        mgr.logs.append("queue", "n-%d" % i)
    assert [line.text for line in mgr.logs.lines("queue")] == ["n-3", "n-4", "n-5"]


def test_child_stdout_is_captured_into_the_buffer(tmp_path):
    cfg = make_config(azctl, tmp_path)
    script = "print('hello from fake'); import time; time.sleep(0.5)"
    mgr = azctl.ServiceManager(
        cfg,
        command_for=lambda name, config: [sys.executable, "-u", "-c", script],
    )
    mgr.start("blob")
    try:
        assert wait_for(
            lambda: any("hello from fake" in ln.text for ln in mgr.logs.lines("blob"))
        )
    finally:
        mgr.shutdown()


# --- port -> PID lookup ---------------------------------------------------


def test_pid_on_port_names_a_listener_we_own():
    port = free_port()
    proc = spawn_listener(port)
    try:
        holder = azctl.pid_on_port(port)
        assert holder is not None
        pid, name = holder
        assert pid == proc.pid
        assert isinstance(name, str) and name
    finally:
        azctl.kill_pid(proc.pid)
        proc.wait(timeout=10)


def test_pid_on_port_is_none_when_nothing_listens():
    assert azctl.pid_on_port(free_port()) is None


# --- kill + post-kill recheck (free_port_flow) ---------------------------


def test_free_port_flow_with_nothing_listening_asks_nothing():
    port = free_port()
    asked = []

    def confirm(prompt):
        asked.append(prompt)
        return True

    ok, msg, style = azctl.free_port_flow(port, confirm, sleep=no_sleep)
    assert ok and style == "grey"
    assert "Nothing is listening on port %d" % port in msg
    assert asked == []  # no confirmation for a no-op


def test_free_port_flow_cancel_kills_nothing():
    port = free_port()
    srv = foreign_listener(port)  # held by the test process: must survive
    try:

        def confirm(prompt):
            assert str(os.getpid()) in prompt and str(port) in prompt
            return False

        ok, msg, style = azctl.free_port_flow(port, confirm, sleep=no_sleep)
        assert not ok and msg == "Cancelled." and style == "grey"
        assert azctl.port_open("127.0.0.1", port)  # still listening
    finally:
        srv.close()


def test_free_port_flow_kills_waits_and_rechecks(tmp_path):
    port = free_port()
    proc = spawn_listener(port)
    sleeps = []
    killed = []
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        return True

    try:
        ok, msg, style = azctl.free_port_flow(
            port,
            confirm,
            sleep=lambda seconds: sleeps.append(seconds),
            on_killed=lambda pid: killed.append(pid),
        )
        assert ok and style == "green"
        assert "PID %d" % proc.pid in msg and "free" in msg
        assert prompts and "PID %d" % proc.pid in prompts[0]
        assert sleeps == [1.5]  # it waited before declaring victory
        assert killed == [proc.pid]
        assert not azctl.port_open("127.0.0.1", port)
        assert wait_for(lambda: proc.poll() is not None, timeout=5)
    finally:
        azctl.kill_pid(proc.pid)


def test_free_port_flow_detects_a_respawned_listener(tmp_path):
    """A supervisor that resurrects the killed process must be reported as a
    failure with the likely cause, not as a success."""
    port = free_port()
    script = tmp_path / "respawner.py"
    script.write_text(RESPAWNER_SCRIPT)
    wrapper = subprocess.Popen([sys.executable, str(script), str(port)])
    try:
        assert wait_for(lambda: azctl.port_open("127.0.0.1", port)), "no listener"
        holder = azctl.pid_on_port(port)
        assert holder is not None and holder[0] is not None
        first_pid = holder[0]
        assert first_pid != wrapper.pid  # the child owns the socket

        def settle(_seconds):
            # Stand-in for the 1.5 s settle wait: wait for the respawn.
            wait_for(lambda: azctl.port_open("127.0.0.1", port), timeout=8)

        ok, msg, style = azctl.free_port_flow(
            port, lambda prompt: True, sleep=settle
        )
        assert not ok and style == "red"
        assert "restarted" in msg and "source" in msg
        assert "PID %d" % first_pid not in msg  # it names the new holder
    finally:
        azctl.kill_pid(wrapper.pid)
        wrapper.wait(timeout=10)


def test_free_port_flow_reports_a_process_that_would_not_die(monkeypatch):
    port = free_port()
    srv = foreign_listener(port)  # our own process; the kill is stubbed out
    monkeypatch.setattr(azctl, "kill_pid", lambda pid, timeout=2.0: True)
    try:
        ok, msg, style = azctl.free_port_flow(
            port, lambda prompt: True, sleep=no_sleep
        )
        assert not ok and style == "red"
        assert "did not die" in msg and "PID %d" % os.getpid() in msg
    finally:
        srv.close()


def test_killing_our_own_child_reads_stopped_not_broken(tmp_path):
    """Free port aimed at the dashboard's own service: the engine is told via
    notice_external_kill and the row flips to stopped."""
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=listener_command, health_timeout=0.1)
    dash = azctl.Dashboard(cfg, mgr, sleep=no_sleep)
    mgr.start("blob")
    try:
        assert wait_for(lambda: refreshed_state(mgr, "blob") == azctl.RUNNING)
        pid = view_of(mgr, "blob").pid
        for _ in range(4):  # Start, Stop, Restart, Save -> Free port
            dash.handle_event(azctl.KeyEvent("right"))
        dash.handle_event(azctl.KeyEvent("enter"))
        assert dash.ui.mode == "confirm"
        assert "PID %d" % pid in dash.ui.pending_prompt
        assert str(cfg.blob_port) in dash.ui.pending_prompt
        dash.handle_event(azctl.KeyEvent("enter"))  # confirm the kill
        assert dash.ui.message is not None and "Killed" in dash.ui.message[0]
        assert wait_for(lambda: refreshed_state(mgr, "blob") == azctl.STOPPED)
        assert view_of(mgr, "blob").state != azctl.BROKEN
    finally:
        mgr.shutdown()


# --- log saving -----------------------------------------------------------


def test_save_service_log_reports_count_and_full_path(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=lambda name, config: None)
    for text in ("alpha", "beta", "gamma"):
        mgr.logs.append("blob", text)
    ok, msg, style = mgr.save_service_log("blob", str(tmp_path))
    assert ok and style == "green"
    path = tmp_path / "azurite-blob.log"
    assert "3 lines" in msg and str(path) in msg
    assert path.read_text() == "alpha\nbeta\ngamma\n"


def test_save_empty_log_still_writes_and_says_so(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=lambda name, config: None)
    ok, msg, style = mgr.save_service_log("queue", str(tmp_path))
    assert ok and style == "yellow"
    assert "0 lines" in msg and "empty" in msg
    assert (tmp_path / "azurite-queue.log").read_text() == ""
    ok, msg, style = mgr.save_merged_log(str(tmp_path))
    assert ok and style == "yellow" and "empty" in msg
    assert (tmp_path / "azurite-all.log").read_text() == ""


def test_save_merged_log_is_tagged_and_in_arrival_order(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=lambda name, config: None)
    mgr.logs.append("blob", "b-first")
    mgr.logs.append("queue", "q-second")
    mgr.logs.append("blob", "b-third")
    ok, msg, style = mgr.save_merged_log(str(tmp_path))
    assert ok and style == "green" and "3 lines" in msg
    lines = (tmp_path / "azurite-all.log").read_text().splitlines()
    assert len(lines) == 3
    assert lines[0].endswith("[blob] b-first")
    assert lines[1].endswith("[queue] q-second")
    assert lines[2].endswith("[blob] b-third")


def test_save_service_log_survives_a_deleted_cwd(monkeypatch, tmp_path):
    """os.getcwd() itself raises FileNotFoundError when the directory azctl
    was launched from has since been removed (rm -rf'd workdir, branch
    switch that dropped it, a cleaned-up tmp dir). Save must still fail with
    the usual red message, not an unhandled traceback."""
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=lambda name, config: None)
    mgr.logs.append("blob", "a line")

    def boom():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(azctl.os, "getcwd", boom)
    ok, msg, style = mgr.save_service_log("blob")  # directory=None -> os.getcwd()
    assert not ok and style == "red" and "Could not" in msg


def test_save_merged_log_survives_a_deleted_cwd(monkeypatch, tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, command_for=lambda name, config: None)
    mgr.logs.append("blob", "a line")

    def boom():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(azctl.os, "getcwd", boom)
    ok, msg, style = mgr.save_merged_log()
    assert not ok and style == "red" and "Could not" in msg


# --- connection strings ---------------------------------------------------


def test_connection_string_builder_per_service():
    cfg = azctl.Config(
        host="127.0.0.1", blob_port=10500, queue_port=10501, table_port=10502
    )
    blob = azctl.connection_string("blob", cfg)
    assert "DefaultEndpointsProtocol=http;" in blob
    assert "AccountName=devstoreaccount1;" in blob
    assert "AccountKey=%s;" % azctl.ACCOUNT_KEY in blob
    assert "BlobEndpoint=http://127.0.0.1:10500/devstoreaccount1;" in blob
    queue = azctl.connection_string("queue", cfg)
    assert "QueueEndpoint=http://127.0.0.1:10501/devstoreaccount1;" in queue
    table = azctl.connection_string("table", cfg)
    assert "TableEndpoint=http://127.0.0.1:10502/devstoreaccount1;" in table


# --- config: file + flags precedence -------------------------------------


def test_flags_beat_file_beats_defaults(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"blob_port": 30000, "queue_port": 31000, "host": "10.0.0.5"})
    )
    file_dict = azctl.load_config(cfg_file)
    args = azctl.build_parser().parse_args(["--blob-port", "20000"])
    cfg = azctl.resolve_config(args, file_dict)
    assert cfg.blob_port == 20000  # flag beats file
    assert cfg.queue_port == 31000  # file beats default
    assert cfg.table_port == 10002  # untouched default
    assert cfg.host == "10.0.0.5"  # file beats default


def test_all_flags_reach_the_config(tmp_path):
    args = azctl.build_parser().parse_args(
        [
            "--host", "0.0.0.0",
            "--blob-port", "1", "--queue-port", "2", "--table-port", "3",
            "--data-dir", str(tmp_path),
        ]
    )
    cfg = azctl.resolve_config(args, {})
    assert (cfg.host, cfg.blob_port, cfg.queue_port, cfg.table_port) == (
        "0.0.0.0", 1, 2, 3,
    )
    assert cfg.data_dir == str(tmp_path)


def test_data_dir_tilde_is_expanded():
    args = azctl.build_parser().parse_args([])
    cfg = azctl.resolve_config(args, {"data_dir": "~/azctl-test-data"})
    assert "~" not in cfg.data_dir
    assert cfg.data_dir == os.path.expanduser("~/azctl-test-data")


def test_load_config_missing_file_is_empty(tmp_path):
    assert azctl.load_config(tmp_path / "nope.json") == {}


def test_load_config_broken_json_warns_and_falls_back(tmp_path, capsys):
    bad = tmp_path / "config.json"
    bad.write_text("{not json")
    assert azctl.load_config(bad) == {}
    assert "warning" in capsys.readouterr().err


def test_load_config_non_object_warns_and_falls_back(tmp_path, capsys):
    bad = tmp_path / "config.json"
    bad.write_text("[1, 2, 3]")
    assert azctl.load_config(bad) == {}
    assert "warning" in capsys.readouterr().err


def test_load_config_bad_port_is_skipped_good_keys_kept(tmp_path, capsys):
    mixed = tmp_path / "config.json"
    mixed.write_text(
        json.dumps({"blob_port": "not-a-port", "queue_port": "10500", "host": "h"})
    )
    out = azctl.load_config(mixed)
    assert out == {"queue_port": 10500, "host": "h"}
    assert "warning" in capsys.readouterr().err


def test_config_path_respects_xdg_and_appdata():
    posix_xdg = azctl.config_path({"XDG_CONFIG_HOME": "/xdg"}, "/home/u", False)
    assert posix_xdg == pathlib.Path("/xdg") / "azctl" / "config.json"
    posix_default = azctl.config_path({}, "/home/u", False)
    assert posix_default == (
        pathlib.Path("/home/u") / ".config" / "azctl" / "config.json"
    )
    windows = azctl.config_path({"APPDATA": "/appdata"}, "/home/u", True)
    assert windows == pathlib.Path("/appdata") / "azctl" / "config.json"


# --- Windows npm .cmd shim (cross-platform spawn correctness) -------------


def test_default_command_wraps_a_windows_cmd_shim(monkeypatch, tmp_path):
    """npm installs azurite-blob.cmd (a batch wrapper) on Windows instead of
    a native exe. subprocess.Popen with shell=False can't exec a .cmd file
    directly (WinError 193) — it must be run through `cmd /c`, the same
    trick Node's own cross-spawn uses for this exact npm-shim problem."""
    monkeypatch.setattr(azctl.os, "name", "nt")
    monkeypatch.setattr(
        azctl.shutil, "which",
        lambda exe: r"C:\npm\azurite-blob.cmd" if exe == "azurite-blob" else None,
    )
    cfg = make_config(azctl, tmp_path)
    cmd = azctl.default_command_for("blob", cfg)
    assert cmd[:2] == ["cmd", "/c"]
    assert cmd[2].lower().endswith("azurite-blob.cmd")
    assert "--blobHost" in cmd


def test_default_command_does_not_wrap_a_posix_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(azctl.os, "name", "posix")
    monkeypatch.setattr(
        azctl.shutil, "which",
        lambda exe: "/usr/local/bin/azurite-blob" if exe == "azurite-blob" else None,
    )
    cfg = make_config(azctl, tmp_path)
    cmd = azctl.default_command_for("blob", cfg)
    assert cmd[0] == "/usr/local/bin/azurite-blob"
    assert cmd[:2] != ["cmd", "/c"]


def test_default_command_does_not_wrap_a_windows_exe(monkeypatch, tmp_path):
    """Only .cmd/.bat shims need the wrapper; a native .exe must not be."""
    monkeypatch.setattr(azctl.os, "name", "nt")
    monkeypatch.setattr(
        azctl.shutil, "which",
        lambda exe: r"C:\npm\azurite-blob.exe" if exe == "azurite-blob" else None,
    )
    cfg = make_config(azctl, tmp_path)
    cmd = azctl.default_command_for("blob", cfg)
    assert cmd[0] == r"C:\npm\azurite-blob.exe"


def test_run_version_wraps_a_windows_cmd_shim(monkeypatch):
    monkeypatch.setattr(azctl.os, "name", "nt")
    monkeypatch.setattr(
        azctl.shutil, "which", lambda exe: r"C:\npm\node.cmd" if exe == "node" else None
    )
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="v20.0.0\n", stderr="")

    monkeypatch.setattr(azctl.subprocess, "run", fake_run)
    assert azctl._run_version("node") == "v20.0.0"
    assert captured["cmd"][:2] == ["cmd", "/c"]
    assert captured["cmd"][2] == r"C:\npm\node.cmd"


# --- real azurite (only when installed) -----------------------------------


@pytest.mark.skipif(
    shutil.which("azurite-blob") is None, reason="azurite is not installed"
)
def test_real_azurite_blob_starts_and_stops(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg, health_timeout=0.2)  # real default_command_for
    ok, msg, _style = mgr.start("blob")
    assert ok, msg
    try:
        assert wait_for(
            lambda: refreshed_state(mgr, "blob") == azctl.RUNNING, timeout=30
        ), "real azurite-blob never answered on its port"
    finally:
        mgr.shutdown()
    assert refreshed_state(mgr, "blob") == azctl.STOPPED
    assert not azctl.port_open(cfg.host, cfg.blob_port)
