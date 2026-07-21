"""Optional integration tests against a real Azurite install.

Skipped automatically when azurite is not on PATH (as in the CI test jobs
that intentionally never install Node/Azurite -- see the bootstrap-smoke
job for the one place CI does).
"""

import re
import shutil
import subprocess
import sys
import time

import pytest
from rich.console import Console
from textual.coordinate import Coordinate

import azctl
from helpers import free_port, make_config, wait_for

pytestmark = pytest.mark.skipif(
    shutil.which("azurite-blob") is None
    or shutil.which("azurite-queue") is None
    or shutil.which("azurite-table") is None,
    reason="azurite is not installed",
)


def test_real_azurite_blob_lifecycle(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg)
    ok, msg, _style = mgr.start("blob")
    assert ok, msg
    try:
        assert wait_for(
            lambda: (mgr.refresh(), mgr.views()[0].state)[1] == azctl.RUNNING,
            timeout=30,
        ), "azurite-blob never reached running"
    finally:
        mgr.shutdown()
    mgr.refresh()
    assert mgr.views()[0].state == azctl.STOPPED
    assert not azctl.port_open(cfg.host, cfg.blob_port)


# --- full dashboard pass against real azurite-{blob,queue,table} -----------
#
# Everything below drives the real AzctlApp (Textual, headless via
# run_test()) wired to a real ServiceManager using the *default*
# command_for -- i.e. the actual /usr/bin/azurite-{blob,queue,table}
# binaries, not a fake/mock. No PATH hacking: shutil.which() finds them
# exactly the way a real user's shell would.


def _azurite_node_pids():
    """PIDs of any real `node .../azurite-{blob,queue,table}` processes.

    Anchored on "node " + the script name so it can never match this test's
    own process (which never has "node" as argv[0] preceding an
    azurite-*/main.js path), unlike a bare `pgrep azurite` would.
    """
    try:
        proc = subprocess.run(
            ["pgrep", "-f", r"node .*azurite-(blob|queue|table)"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        pytest.skip("pgrep is not available on this machine")
    return [int(p) for p in proc.stdout.split()]


def _export(renderable, width=160):
    console = Console(record=True, width=width)
    console.print(renderable)
    return console.export_text()  # styles=False: plain text, ANSI stripped


def _table_cell(app, row, col):
    table = app.query_one("#table")
    return table.get_cell_at(Coordinate(row, col))


@pytest.mark.skipif(sys.platform == "win32", reason="pgrep-based orphan check is POSIX-only")
async def test_real_azurite_dashboard_full_lifecycle(tmp_path):
    """End-to-end pass with NO fakes and NO mocks anywhere in the chain:

    1. Start all three real azurite-{blob,queue,table} processes through
       AzctlApp (start_all_on_entry=True, same as `azctl up`), and poll the
       real DataTable until every row shows "running".
    2. Confirm the header ends up showing a real detected azurite version
       (an "X.Y.Z" string, not "unknown").
    3. Toggle the merged log view ('a') and confirm real Azurite "...
       successfully listens ..." lines show up tagged with their service
       and in arrival order.
    4. Quit via the three-way modal choosing "stop all" (Enter), and
       confirm the shutdown left no orphaned azurite process behind
       (checked both with pgrep and with psutil.pid_exists on the exact
       PIDs we started).

    Every wait below is bounded by an explicit timeout; nothing can hang.
    """
    baseline_orphans = set(_azurite_node_pids())

    config = azctl.Config(
        blob_port=free_port(),
        queue_port=free_port(),
        table_port=free_port(),
        data_dir=str(tmp_path),
    )
    manager = azctl.ServiceManager(config)  # default command_for: real azurite-* on PATH
    app = azctl.AzctlApp(config, manager, start_all_on_entry=True)

    started_pids = set()
    try:
        async with app.run_test() as pilot:
            await pilot.pause()

            # 1. real processes reach RUNNING (checked at the engine level,
            # which is what the table renders from). manager.refresh() only
            # ever runs from AzctlApp's own 0.1s tick, which is driven by the
            # asyncio loop -- so this has to be a pilot.pause() poll loop
            # (yielding control back to that loop each iteration), NOT a
            # blocking time.sleep-based wait_for, or the tick would never
            # get a chance to run and the state would never leave "starting".
            running_ready = False
            for _ in range(450):  # up to 45s of real node + azurite startup
                await pilot.pause(0.1)
                if all(v.state == azctl.RUNNING for v in manager.views()):
                    running_ready = True
                    break
            assert running_ready, [(v.name, v.state) for v in manager.views()]
            started_pids = {v.pid for v in manager.views() if v.pid is not None}
            assert len(started_pids) == 3

            # Now let the DataTable itself catch up and confirm the SAME
            # thing at the UI level (poll -- the refresh tick is 0.1s).
            table_ready = False
            expected = "%s %s" % (azctl.STATE_SYMBOLS[azctl.RUNNING], azctl.RUNNING)
            for _ in range(100):
                await pilot.pause(0.1)
                if all(_table_cell(app, i, 1).plain == expected for i in range(3)):
                    table_ready = True
                    break
            assert table_ready, "DataTable never showed all three rows as running"

            # 2. header shows a real azurite version -------------------------
            version_seen = False
            for _ in range(150):  # up to 15s: _probe_versions runs on a daemon thread
                await pilot.pause(0.1)
                if re.match(r"^\d+\.\d+\.\d+", app.ui.versions[0]):
                    version_seen = True
                    break
            assert version_seen, "header never showed a real azurite version: %r" % (app.ui.versions,)
            header_text = _export(app.query_one("#header").content)
            assert re.search(r"azurite \d+\.\d+\.\d+", header_text), header_text

            # 3. merged log view: real listen lines, tagged + arrival-ordered -
            #
            # Azurite 3.36.0's own wording differs slightly by service once a
            # listener is actually up: Blob/Queue print "successfully listens
            # on http://host:port" but Table prints "successfully started on
            # host:port" (no "http://"). Build the exact real marker per
            # service instead of assuming a single shared phrase.
            await pilot.press("a")

            def _listen_marker(name):
                port = config.port_for(name)
                if name == "table":
                    return "successfully started on %s:%d" % (config.host, port)
                return "successfully listens on http://%s:%d" % (config.host, port)

            markers = {name: _listen_marker(name) for name in azctl.SERVICE_ORDER}
            merged_ready = False
            for _ in range(200):  # up to 20s of real node startup output
                await pilot.pause(0.1)
                lines = manager.logs.merged()
                have = {name: any(marker in ln.text for ln in lines) for name, marker in markers.items()}
                if all(have.values()):
                    merged_ready = True
                    break
            assert merged_ready, "not all three real 'successfully listens' lines arrived"

            merged_lines = manager.logs.merged()
            arrival_order = [
                name
                for line in merged_lines
                for name, marker in markers.items()
                if marker in line.text
            ]
            assert set(arrival_order) == set(azctl.SERVICE_ORDER)

            out = _export(app.query_one("#logpanel").content)
            for name in azctl.SERVICE_ORDER:
                assert "[%s] " % name in out, out
            # The rendered order must match the merged store's arrival order.
            positions = [out.index(markers[name]) for name in arrival_order]
            assert positions == sorted(positions), (arrival_order, positions, out)

            # 4. quit via the three-way modal, choosing "stop all" -----------
            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, azctl.QuitScreen)
            await pilot.press("enter")  # -> "stop"
            deadline = time.monotonic() + 20
            while app.is_running and time.monotonic() < deadline:
                await pilot.pause(0.2)
            assert not app.is_running, "app never exited after choosing stop-all"
            assert app.exit_note == "Stopped all services."
    finally:
        manager.shutdown()  # idempotent safety net, mirrors run_dashboard()

    # Shutdown must have left no orphaned azurite process anywhere:
    assert wait_for(lambda: not (set(_azurite_node_pids()) - baseline_orphans), timeout=10), (
        "orphaned azurite process(es) after quit: %r"
        % (set(_azurite_node_pids()) - baseline_orphans,)
    )
    import psutil

    for pid in started_pids:
        assert not psutil.pid_exists(pid), "PID %d (one of our own children) is still alive" % pid
    assert not azctl.port_open(config.host, config.blob_port)
    assert not azctl.port_open(config.host, config.queue_port)
    assert not azctl.port_open(config.host, config.table_port)
