"""Headless Textual dashboard tests for AzctlApp.

Everything runs without a TTY (Textual's run_test() is headless), without
azurite, and without launching a real interactive event loop: AzctlApp is
driven through ``async with app.run_test() as pilot``, wired either to a
FakeManager (fast, deterministic) or a real ServiceManager pointed at a
fake ``python3 -c`` TCP-listener command injected through the
``command_for`` seam (helpers.listener_command) -- never a real Azurite
install.

Coverage is mapped to BEHAVIOR.md: the five-state table, selection/log-panel
following, button-highlight styling, Start/Stop/Restart/Save/Free-port/Stop-all
button semantics and their confirmations, the merged-log and timestamp
toggles, connection-string copy, the help overlay, the three-way quit
question, the broken-transition bell, and the non-TTY refusal.
"""

import asyncio
import base64
import io
import os
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager

import pytest
from rich.console import Console
from textual.coordinate import Coordinate

import azctl
from helpers import free_port, listener_command, spawn_listener, wait_busy_clears, wait_for


@asynccontextmanager
async def dashboard(app):
    """Run `app` headlessly via Pilot, then force a clean `app.exit()`
    *before* letting `run_test()`'s own teardown proceed.

    AzctlApp's periodic 0.1 s refresh timer (_on_tick, installed via
    set_interval in on_mount) now guards against firing mid-teardown (see
    `test_refresh_timer_does_not_race_app_teardown` near the bottom of this
    file), but calling exit() explicitly here still lets Textual's own exit
    machinery stop the timer deterministically before Pilot's teardown, which
    keeps every other test in this module from paying that (small, now
    harmless) race at all.
    """
    async with app.run_test() as pilot:
        try:
            yield pilot
        finally:
            if app.is_running:
                app.exit()
            await pilot.pause()


# --- render/export helpers -----------------------------------------------


def export(renderable, width=140):
    # file=io.StringIO(): a record=True Console still physically writes to
    # its `file` in addition to recording. Without an explicit in-memory
    # sink it defaults to sys.stdout -- which, while an AzctlApp is running
    # under run_test(), Textual transparently redirects to its own App._print
    # so it can capture stray prints. On a Windows CI runner that redirect's
    # real underlying stream uses the legacy 'cp1252' console codepage, which
    # can't encode the box-drawing/bullet glyphs these renderables use,
    # raising UnicodeEncodeError -- a test-tooling bug (a helper accidentally
    # depending on the real stdout's encoding), not anything about AzctlApp's
    # own rendering.
    console = Console(record=True, width=width, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def view(name, state, port, pid=None, uptime=None, ever=False, exit_code=None):
    return azctl.ServiceView(name, state, port, pid, uptime, ever, exit_code)


# --- a minimal fake ServiceManager: just enough surface for AzctlApp ----


class FakeManager:
    """Fakes ServiceManager's public surface so AzctlApp can be driven
    without real processes when a test only cares about UI wiring."""

    def __init__(self, owned=(), views=None):
        self.logs = azctl.LogStore()
        self.owned = set(owned)
        self.calls = []
        self._views = views

    def owns_live(self, name):
        return name in self.owned

    def any_owned(self):
        return bool(self.owned)

    def refresh(self):
        return []

    def views(self):
        if self._views is not None:
            return self._views
        return [
            view(
                name,
                azctl.RUNNING if name in self.owned else azctl.STOPPED,
                10000 + i,
                pid=100 + i if name in self.owned else None,
                uptime=1.0 if name in self.owned else None,
                ever=name in self.owned,
            )
            for i, name in enumerate(azctl.SERVICE_ORDER)
        ]

    def broken_reason(self, name):
        return None

    def start(self, name):
        self.calls.append(("start", name))
        self.owned.add(name)
        return True, "Started %s (PID 1)." % name.capitalize(), "green"

    def stop(self, name):
        self.calls.append(("stop", name))
        if name not in self.owned:
            return True, "%s is not running." % name.capitalize(), "grey"
        self.owned.discard(name)
        return True, "Stopped %s." % name.capitalize(), "green"

    def restart(self, name):
        self.calls.append(("restart", name))
        return True, "Restarted %s." % name.capitalize(), "green"

    def start_all(self):
        return [self.start(name) for name in azctl.SERVICE_ORDER]

    def stop_all(self):
        return [self.stop(name) for name in azctl.SERVICE_ORDER]

    def shutdown(self):
        self.calls.append(("shutdown",))
        self.owned.clear()

    def detach_all(self):
        self.calls.append(("detach_all",))
        self.owned.clear()

    def save_service_log(self, name, directory=None):
        self.calls.append(("save", name))
        return True, "Wrote 0 lines to azurite-%s.log (log was empty)" % name, "yellow"

    def save_merged_log(self, directory=None):
        self.calls.append(("save_merged",))
        return True, "Wrote 0 lines to azurite-all.log (log was empty)", "yellow"

    def notice_external_kill(self, name):
        self.calls.append(("notice_external_kill", name))


class TransitionManager(FakeManager):
    """Replays a fixed list of Transitions, one per refresh() call --
    lets a test drive AzctlApp's refresh tick into a ->BROKEN transition
    without a real process dying."""

    def __init__(self, transitions, owned=(), reason=None):
        FakeManager.__init__(self, owned=owned)
        self._transitions = list(transitions)
        self._reason = reason

    def refresh(self):
        if self._transitions:
            return [self._transitions.pop(0)]
        return []

    def broken_reason(self, name):
        return self._reason


def table_cell(app, row, col):
    table = app.query_one("#table")
    return table.get_cell_at(Coordinate(row, col))


def footer_text(app, width=140):
    return export(app.query_one("#footer").content, width=width)


def logpanel_text(app, width=140):
    return export(app.query_one("#logpanel").content, width=width)


# --- five-state table rendering ------------------------------------------


async def test_five_states_symbols_and_colours_in_table():
    views_a = [
        view("blob", azctl.RUNNING, 10000, pid=4242, uptime=3725.0, ever=True),
        view("queue", azctl.STARTING, 10001, pid=4243, uptime=2.0, ever=True),
        view("table", azctl.STOPPED, 10002),
    ]
    app = azctl.AzctlApp(azctl.Config(), FakeManager(views=views_a))
    async with dashboard(app) as pilot:
        await pilot.pause()
        assert table_cell(app, 0, 1).plain == "● running"
        assert table_cell(app, 0, 1).style == "green"
        assert table_cell(app, 1, 1).plain == "◐ starting"
        assert table_cell(app, 1, 1).style == "yellow"
        assert table_cell(app, 2, 1).plain == "○ stopped"
        assert table_cell(app, 2, 1).style == "grey50"
        # port / PID / uptime formatting
        assert table_cell(app, 0, 2) == "10000"
        assert table_cell(app, 0, 3) == "4242"
        assert table_cell(app, 0, 4) == "1:02:05"
        assert table_cell(app, 2, 3) == "—"
        assert table_cell(app, 2, 4) == "—"

    views_b = [
        view("blob", azctl.BROKEN, 10000, ever=True, exit_code=3),
        view("queue", azctl.PORT_IN_USE, 10001),
        view("table", azctl.STOPPED, 10002),
    ]
    app2 = azctl.AzctlApp(azctl.Config(), FakeManager(views=views_b))
    async with dashboard(app2) as pilot:
        await pilot.pause()
        assert table_cell(app2, 0, 1).plain == "✖ broken"
        assert table_cell(app2, 0, 1).style == "red"
        assert table_cell(app2, 1, 1).plain == "◆ port in use"
        assert table_cell(app2, 1, 1).style == "magenta"


async def test_selected_marker_is_the_datatable_cursor_row():
    app = azctl.AzctlApp(azctl.Config(), FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        table = app.query_one("#table")
        assert table.cursor_row == 0
        await pilot.press("down")
        await pilot.pause()
        assert table.cursor_row == 1
        assert app.ui.selected == 1
        await pilot.press("down")
        await pilot.pause()
        assert table.cursor_row == 2
        await pilot.press("down")  # wraps back to 0
        await pilot.pause()
        assert table.cursor_row == 0


# --- up/down selection + the log panel following it ----------------------


async def test_up_down_selects_service_and_log_panel_follows():
    manager = FakeManager()
    manager.logs.append("blob", "hello from blob")
    manager.logs.append("queue", "hello from queue")
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        assert "hello from blob" in logpanel_text(app)
        await pilot.press("down")
        await pilot.pause(0.15)  # let the next refresh tick redraw the log panel
        assert app.ui.selected == 1
        assert app.ui.message[0] == "Selected Queue."
        assert app.ui.message[1] == "grey"
        assert "hello from queue" in logpanel_text(app)
        assert "hello from blob" not in logpanel_text(app)
        await pilot.press("up")
        await pilot.pause(0.15)
        assert app.ui.selected == 0
        assert app.ui.message[0] == "Selected Blob."


# --- left/right button highlight + destructive styling --------------------


async def test_left_right_moves_button_highlight_and_destructive_are_styled():
    app = azctl.AzctlApp(azctl.Config(), FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        assert app.ui.button == 0
        text = footer_text(app)
        assert "[Stop]" in text and "[Restart]" in text and "[Free port]" in text
        assert "[Stop all]" in text
        await pilot.press("right")
        await pilot.pause()
        assert app.ui.button == 1
        await pilot.press("left")
        await pilot.pause()
        assert app.ui.button == 0
        # Clamped at the left edge.
        await pilot.press("left")
        await pilot.pause()
        assert app.ui.button == 0
        # Clamped at the right edge (7 buttons, indices 0..6).
        for _ in range(10):
            await pilot.press("right")
            await pilot.pause()
        assert app.ui.button == len(azctl.BUTTONS) - 1


def test_button_bar_destructive_buttons_render_in_red():
    """Same semantics AzctlApp's footer draws from render_button_bar --
    verified directly against the pure function (already unit-tested in
    test_render.py) so this file also documents the exact BEHAVIOR.md claim
    the App-level test above depends on."""
    bar = azctl.render_button_bar(0)
    console = Console(record=True, width=140, file=io.StringIO())
    console.print(bar)
    segments = list(console.render(bar))
    danger_labels = [b.label for b in azctl.BUTTONS if b.danger]
    for seg in segments:
        if seg.text.strip("[]") in danger_labels and seg.style is not None:
            assert seg.style.color is not None and seg.style.color.name == "red"


# --- Enter on Start: no confirm, real fake service ------------------------


async def test_enter_on_start_starts_the_service_no_confirm():
    manager = FakeManager()
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("enter")  # button 0 = Start, no confirm needed
        await pilot.pause()
        assert ("start", "blob") in manager.calls
        assert app.ui.message[0] == "Started Blob (PID 1)."
        assert app.ui.message[1] == "green"
        assert not isinstance(app.screen, azctl.ConfirmScreen)  # Start never confirms


async def test_start_reaches_running_with_a_real_fake_listener_process(tmp_path):
    """Uses a real ServiceManager wired to a fake `python3 -c` TCP-listener
    command (helpers.listener_command) instead of a mock -- no azurite
    binary anywhere, but a genuine child process and a genuine port."""
    config = azctl.Config(
        blob_port=free_port(), queue_port=free_port(), table_port=free_port(), data_dir=str(tmp_path)
    )
    manager = azctl.ServiceManager(config, command_for=listener_command, health_timeout=0.1)
    app = azctl.AzctlApp(config, manager)
    try:
        async with dashboard(app) as pilot:
            await pilot.pause()
            await pilot.press("enter")  # Start Blob
            await pilot.pause()
            assert "Started Blob" in app.ui.message[0]
            assert app.ui.message[1] == "green"
            assert wait_for(lambda: manager.views()[0].state != azctl.STOPPED, timeout=5)
            for _ in range(30):
                await pilot.pause(0.1)
                if manager.views()[0].state == azctl.RUNNING:
                    break
            assert manager.views()[0].state == azctl.RUNNING
    finally:
        manager.shutdown()


# --- Stop/Restart: confirm modal, Enter confirms, other key cancels ------


async def test_stop_asks_confirm_and_enter_confirms_it():
    manager = FakeManager(owned={"blob"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("right")  # button 1 = Stop
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, azctl.ConfirmScreen)
        assert app.screen.prompt == "Stop Blob?"
        await pilot.press("enter")  # confirm
        assert await wait_busy_clears(pilot, app)
        assert ("stop", "blob") in manager.calls
        assert app.ui.message == ("Stopped Blob.", "green", app.ui.message[2])
        assert not isinstance(app.screen, azctl.ConfirmScreen)


async def test_confirm_any_non_enter_key_cancels_with_message():
    for other_key in ("escape", "z", "a", "q", "left", "up"):
        manager = FakeManager(owned={"blob"})
        app = azctl.AzctlApp(azctl.Config(), manager)
        async with dashboard(app) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, azctl.ConfirmScreen)
            await pilot.press(other_key)
            await pilot.pause()
            assert ("stop", "blob") not in manager.calls, other_key
            assert app.ui.message[0] == "Cancelled.", other_key
            assert app.ui.message[1] == "grey", other_key
            assert not isinstance(app.screen, azctl.ConfirmScreen), other_key


async def test_restart_asks_confirm_named_for_the_selected_service():
    manager = FakeManager(owned={"queue"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("down")  # select Queue
        await pilot.press("right", "right")  # button 2 = Restart
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, azctl.ConfirmScreen)
        assert app.screen.prompt == "Restart Queue?"
        await pilot.press("enter")
        assert await wait_busy_clears(pilot, app)
        assert ("restart", "queue") in manager.calls


async def test_stop_when_not_running_does_not_ask_grey_message():
    manager = FakeManager()  # nothing owned
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("right")  # Stop
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, azctl.ConfirmScreen)
        assert "not running" in app.ui.message[0]
        assert app.ui.message[1] == "grey"


# --- Free port: names the squatter, and the no-op path --------------------


async def test_free_port_confirm_names_squatter_and_kill_frees_it():
    port = free_port()
    squatter = spawn_listener(port)
    try:
        config = azctl.Config(blob_port=port, queue_port=free_port(), table_port=free_port())
        app = azctl.AzctlApp(config, FakeManager())
        async with dashboard(app) as pilot:
            await pilot.pause()
            await pilot.press("right", "right", "right", "right")  # button 4 = Free port
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, azctl.ConfirmScreen)
            match = re.fullmatch(r"Kill (.+) \(PID (\d+)\) on port (\d+)\?", app.screen.prompt)
            assert match, app.screen.prompt
            assert int(match.group(2)) == squatter.pid
            assert int(match.group(3)) == port
            await pilot.press("enter")  # confirm the kill
            assert await wait_busy_clears(pilot, app, timeout=15)
            assert app.ui.message[1] == "green"
            assert str(squatter.pid) in app.ui.message[0]
            assert wait_for(lambda: squatter.poll() is not None, timeout=10)
    finally:
        if squatter.poll() is None:
            squatter.terminate()
            squatter.wait(timeout=5)


async def test_free_port_with_nothing_listening_is_a_grey_noop():
    config = azctl.Config(blob_port=free_port(), queue_port=free_port(), table_port=free_port())
    app = azctl.AzctlApp(config, FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("right", "right", "right", "right")  # Free port
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, azctl.ConfirmScreen)
        assert app.ui.message[0] == "Nothing is listening on port %d." % config.blob_port
        assert app.ui.message[1] == "grey"


# --- Stop all: confirm, grey message when nothing running -----------------


async def test_stop_all_confirms_then_stops_everything():
    manager = FakeManager(owned={"blob", "queue"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        for _ in range(6):
            await pilot.press("right")
        assert app.ui.button == len(azctl.BUTTONS) - 1  # Stop all
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, azctl.ConfirmScreen)
        assert app.screen.prompt == "Stop all services?"
        await pilot.press("enter")
        assert await wait_busy_clears(pilot, app)
        assert ("stop", "blob") in manager.calls
        assert ("stop", "queue") in manager.calls


async def test_stop_all_with_nothing_running_is_a_grey_noop():
    manager = FakeManager()
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        for _ in range(6):
            await pilot.press("right")
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, azctl.ConfirmScreen)
        assert app.ui.message[0] == "No services are running."
        assert app.ui.message[1] == "grey"


# --- Save: line count + full path, empty buffer still writes -------------


def test_save_service_log_reports_count_and_path_and_writes_when_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = azctl.Config(blob_port=free_port(), queue_port=free_port(), table_port=free_port())
    manager = azctl.ServiceManager(config, command_for=lambda _n, _c: None)
    manager.logs.append("blob", "line one")
    manager.logs.append("blob", "line two")

    ok, msg, style = manager.save_service_log("blob")
    assert ok and style == "green"
    path = os.path.join(str(tmp_path), "azurite-blob.log")
    assert msg == "Wrote 2 lines to %s" % path
    with open(path) as fh:
        assert fh.read() == "line one\nline two\n"

    ok, msg, style = manager.save_service_log("queue")  # never had any lines
    assert ok and style == "yellow"
    empty_path = os.path.join(str(tmp_path), "azurite-queue.log")
    assert msg == "Wrote 0 lines to %s (log was empty)" % empty_path
    assert os.path.exists(empty_path)


async def test_save_button_reports_via_the_app_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = azctl.Config(blob_port=free_port(), queue_port=free_port(), table_port=free_port())
    manager = azctl.ServiceManager(config, command_for=lambda _n, _c: None)
    manager.logs.append("blob", "only line")
    app = azctl.AzctlApp(config, manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("right", "right", "right")  # button 3 = Save
        await pilot.press("enter")
        await pilot.pause()
        path = os.path.join(str(tmp_path), "azurite-blob.log")
        assert app.ui.message == ("Wrote 1 lines to %s" % path, "green", app.ui.message[2])
        assert os.path.exists(path)


# --- 'a' merged view: arrival order, colour-tagged, footer shows mode ----


async def test_merge_toggle_shows_arrival_ordered_colour_tagged_lines():
    manager = FakeManager()
    manager.logs.append("blob", "blob-first")
    manager.logs.append("queue", "queue-second")
    manager.logs.append("blob", "blob-third")
    manager.logs.append("table", "table-fourth")
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        assert "logs: selected" in footer_text(app)
        await pilot.press("a")
        await pilot.pause(0.15)  # let the next refresh tick redraw footer + log panel
        assert app.ui.combined_logs is True
        assert "logs: ALL" in footer_text(app)
        out = logpanel_text(app)
        assert "[blob] blob-first" in out
        assert "[queue] queue-second" in out
        assert "[table] table-fourth" in out
        positions = [out.index(t) for t in ("blob-first", "queue-second", "blob-third", "table-fourth")]
        assert positions == sorted(positions)
        await pilot.press("a")  # toggle back off
        await pilot.pause(0.15)
        assert app.ui.combined_logs is False
        assert "logs: selected" in footer_text(app)


# --- 't' timestamp toggle --------------------------------------------------


async def test_timestamp_toggle_changes_rendered_log_lines():
    import time as time_mod

    manager = FakeManager()
    wall = time_mod.time() - 4321
    manager.logs.append("blob", "hello world")
    line = manager.logs.lines("blob")[0]
    line.wall = wall  # pin the arrival time for a deterministic stamp
    stamp = time_mod.strftime("%H:%M:%S", time_mod.localtime(wall))
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        assert stamp not in logpanel_text(app)
        await pilot.press("t")
        await pilot.pause(0.15)  # let the next refresh tick redraw the log panel
        assert app.ui.timestamps is True
        assert app.ui.message[0] == "Timestamps on."
        assert "%s hello world" % stamp in logpanel_text(app)
        await pilot.press("t")
        await pilot.pause(0.15)
        assert app.ui.message[0] == "Timestamps off."
        assert stamp not in logpanel_text(app)


# --- 'c' connection string copy (OSC 52) + overlay -------------------------


async def test_c_copies_valid_osc52_of_the_selected_connection_string(monkeypatch):
    copied = []
    monkeypatch.setattr(azctl, "copy_osc52", lambda text: copied.append(text))
    config = azctl.Config(blob_port=free_port(), queue_port=free_port(), table_port=free_port())
    app = azctl.AzctlApp(config, FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("down")  # select Queue
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, azctl.ConnScreen)
        assert copied == [azctl.connection_string("queue", config)]
        assert app.ui.message[1] == "green"
        assert "Queue" in app.ui.message[0]
        await pilot.press("z")  # any key closes
        await pilot.pause()
        assert not isinstance(app.screen, azctl.ConnScreen)


def test_osc52_payload_decodes_to_the_connection_string():
    text = azctl.connection_string("blob", azctl.Config(blob_port=19191))
    sink = io.StringIO()
    azctl.copy_osc52(text, stream=sink)
    raw = sink.getvalue()
    match = re.fullmatch(r"\x1b\]52;c;([A-Za-z0-9+/=]+)\x07", raw)
    assert match, repr(raw)
    decoded = base64.b64decode(match.group(1).encode("ascii"), validate=True).decode("utf-8")
    assert decoded == text


# --- '?' help overlay lists every key --------------------------------------


async def test_help_overlay_lists_every_key_and_any_key_closes_it():
    app = azctl.AzctlApp(azctl.Config(), FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, azctl.HelpScreen)
        out = export(app.screen.query_one(azctl._ModalBody).content, width=140)
        for needle in (
            "↑ / ↓", "← / →", "Enter", "a", "t", "c", "S", "?", "Esc", "q", "mouse",
            "Start", "Stop", "Restart", "Save", "Free port", "Start all", "Stop all",
        ):
            assert needle in out, needle
        await pilot.press("x")
        await pilot.pause()
        assert not isinstance(app.screen, azctl.HelpScreen)


# --- three-way quit modal ---------------------------------------------------


async def test_quit_with_nothing_owned_is_quiet():
    app = azctl.AzctlApp(azctl.Config(), FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running is False
        assert app.exit_note is None


async def test_quit_enter_stops_all_and_exits():
    manager = FakeManager(owned={"blob", "queue"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, azctl.QuitScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert ("shutdown",) in manager.calls
        assert app.is_running is False
        assert app.detached is False
        assert app.exit_note == "Stopped all services."


async def test_quit_n_detaches_and_exits_with_note():
    manager = FakeManager(owned={"blob"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert ("detach_all",) in manager.calls
        assert ("shutdown",) not in manager.calls
        assert app.is_running is False
        assert app.detached is True
        assert app.exit_note == "Left the services running — azctl no longer owns them."


async def test_quit_escape_stays_in_the_dashboard():
    manager = FakeManager(owned={"blob"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, azctl.QuitScreen)
        assert app.is_running is True
        assert ("shutdown",) not in manager.calls
        assert ("detach_all",) not in manager.calls


async def test_quit_prompt_ignores_every_other_key():
    manager = FakeManager(owned={"blob"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        for other_key in ("x", "q", "a", "up", "left"):
            await pilot.press(other_key)
            await pilot.pause()
            assert isinstance(app.screen, azctl.QuitScreen), other_key
            assert app.is_running is True, other_key
        assert ("shutdown",) not in manager.calls
        assert ("detach_all",) not in manager.calls


# --- bell on a ->broken transition ------------------------------------------


async def test_bell_rings_and_broken_reason_shows_on_transition_to_broken():
    manager = TransitionManager(
        [azctl.Transition("blob", azctl.STARTING, azctl.BROKEN)],
        reason="Node.js runtime not found — install Node, then npm install -g azurite",
    )
    app = azctl.AzctlApp(azctl.Config(), manager)
    bells = []
    app.bell = lambda: bells.append(True)
    async with dashboard(app) as pilot:
        await pilot.pause(0.5)  # tick gate fires every 3rd 0.1s beat
        assert bells, "self.bell() was never called on the ->broken transition"
        assert "Node.js runtime not found" in app.ui.message[0]
        assert app.ui.message[1] == "red"


async def test_no_bell_on_a_non_broken_transition():
    manager = TransitionManager([azctl.Transition("blob", azctl.STARTING, azctl.RUNNING)])
    app = azctl.AzctlApp(azctl.Config(), manager)
    bells = []
    app.bell = lambda: bells.append(True)
    async with dashboard(app) as pilot:
        await pilot.pause(0.5)
        assert not bells


# --- non-TTY refusal: exit 2, no App ever constructed ----------------------


def test_non_tty_dashboard_invocation_refuses_politely():
    azctl_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "azctl.py")
    proc = subprocess.run(
        [sys.executable, azctl_path, "up"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 2
    assert "interactive terminal" in proc.stdout
    assert "azctl status" in proc.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX-style piped, non-tty invocation")
def test_non_tty_bare_dashboard_invocation_also_refuses():
    azctl_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "azctl.py")
    proc = subprocess.run(
        [sys.executable, azctl_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 2


# --- regression: AzctlApp's refresh timer must not race Pilot teardown ----
#
# Found while writing this file: AzctlApp.on_mount() installs
# `self.set_interval(0.1, self._on_tick)`, and Textual clears `self._running`
# at the very start of its own `_shutdown()` but before the rest of teardown
# (unmounting widgets) completes. A tick that lands in that window used to
# reach `_refresh_widgets()` and query a widget that was already gone,
# raising `textual.css.query.NoMatches` out of `run_test()`. Fixed in
# `_on_tick` by bailing out once `self.is_running` is False (plus a
# belt-and-braces `except NoMatches` around the refresh call itself for the
# same race). This test drives 40 create/mount/teardown cycles back-to-back,
# without ever pausing for the timer first, specifically to give that window
# every chance to reproduce.
async def test_refresh_timer_does_not_race_app_teardown():
    for _ in range(40):
        app = azctl.AzctlApp(azctl.Config(), FakeManager())
        async with app.run_test() as pilot:  # deliberately NOT the dashboard() helper
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()


# --- review-fix regressions ------------------------------------------------
# Each test below is a regression for one CONFIRMED finding from the review
# of the Textual port.


# Finding: DataTable only posts RowSelected when the click lands on the row
# the cursor is ALREADY on -- the first click on a *different* row only moves
# the cursor and posts RowHighlighted instead, which AzctlApp used to ignore
# entirely. A single click on a row other than the current selection must
# update ui.selected immediately (no second click, no waiting on a timer).
async def test_mouse_click_on_a_different_row_selects_it_in_one_click():
    app = azctl.AzctlApp(azctl.Config(), FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        table = app.query_one("#table")
        assert app.ui.selected == 0
        await pilot.click("#table", offset=(2, table.header_height + 2))  # row 2 = Table
        await pilot.pause()
        assert app.ui.selected == 2
        assert app.ui.message[0] == "Selected Table."


# Same finding as above: the periodic _populate_table() refresh (clear() then
# move_cursor back to ui.selected) posts its own RowHighlighted echoes every
# ~0.3s. Those must never be mistaken for a real click and silently snap the
# selection back -- drive several refresh ticks after a click and make sure
# the click sticks.
async def test_selection_survives_repeated_refresh_ticks_after_a_click():
    app = azctl.AzctlApp(azctl.Config(), FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        table = app.query_one("#table")
        await pilot.click("#table", offset=(2, table.header_height + 2))
        await pilot.pause()
        assert app.ui.selected == 2
        await pilot.pause(0.9)  # several 0.1s ticks, well past the 3rd-tick refresh gate
        assert app.ui.selected == 2
        assert table.cursor_row == 2


# Finding: ConfirmScreen/QuitScreen rendered as an unstyled strip pinned to
# the TOP of the screen -- ModalScreen's own CSS only dims the background, it
# does not position content -- contradicting BEHAVIOR.md's "The question
# appears in the footer." The modal body must dock to the bottom, landing at
# the same y as the real #footer, not y=0.
async def test_confirm_and_quit_modals_dock_in_the_footer_region():
    manager = FakeManager(owned={"blob"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        footer_y = app.query_one("#footer").region.y
        assert footer_y > 0

        await pilot.press("right")  # Stop
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, azctl.ConfirmScreen)
        body = app.screen.query_one(azctl._ModalBody)
        assert body.styles.dock == "bottom"
        assert body.region.y == footer_y
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, azctl.QuitScreen)
        body = app.screen.query_one(azctl._ModalBody)
        assert body.styles.dock == "bottom"
        assert body.region.y == footer_y


# Finding: Ctrl+Q was never bound, so Textual's own built-in
# Binding("ctrl+q", "quit", priority=True) fired instead and exited with
# zero confirmation. Ctrl+C hard-coded an unconditional shutdown with no
# confirmation either. Both are now aliases for 'q': they must raise the same
# three-way stop/detach/stay question rather than silently tearing down (or
# leaking) every owned service.
async def test_ctrl_c_and_ctrl_q_both_trigger_the_three_way_quit_confirmation():
    for key in ("ctrl+c", "ctrl+q"):
        manager = FakeManager(owned={"blob"})
        app = azctl.AzctlApp(azctl.Config(), manager)
        async with dashboard(app) as pilot:
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()
            assert isinstance(app.screen, azctl.QuitScreen), key
            assert app.is_running is True, key
            assert manager.calls == [], key
            await pilot.press("enter")  # confirm: stop + exit
            await pilot.pause()
            for _ in range(100):
                await pilot.pause()
                if not app.is_running:
                    break
                await asyncio.sleep(0.02)
            assert ("shutdown",) in manager.calls, key
            assert app.exit_note == "Stopped all services.", key


async def test_ctrl_c_and_ctrl_q_with_nothing_owned_quit_quietly():
    for key in ("ctrl+c", "ctrl+q"):
        app = azctl.AzctlApp(azctl.Config(), FakeManager())
        async with dashboard(app) as pilot:
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()
            assert app.is_running is False, key
            assert app.exit_note is None, key


# Finding: the '?' help overlay claimed to list every bound key but never
# mentioned Ctrl+C or Ctrl+Q, even though Ctrl+C was live (and unconditionally
# destructive before this fix) the whole time.
async def test_help_overlay_mentions_ctrl_c_and_ctrl_q():
    app = azctl.AzctlApp(azctl.Config(), FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        out = export(app.screen.query_one(azctl._ModalBody).content, width=140)
        assert "Ctrl+C" in out
        assert "Ctrl+Q" in out


# Finding: manager.shutdown() (kill_pid + proc.wait, up to ~3s per stuck
# service) ran directly on the app's asyncio thread on every exit path,
# freezing the whole UI -- rendering, input, timers -- for the duration.
# Drive a real quit through a (fake, but deliberately slow) shutdown and
# assert a parallel high-frequency heartbeat keeps advancing throughout, i.e.
# the event loop was never blocked.
async def test_quit_keeps_the_event_loop_responsive_while_shutdown_is_slow():
    class SlowShutdownManager(FakeManager):
        def shutdown(self):
            time.sleep(0.5)  # simulates a stubborn child ignoring SIGTERM
            FakeManager.shutdown(self)

    manager = SlowShutdownManager(owned={"blob", "queue", "table"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        ticks = []

        async def heartbeat():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.02)

        hb = asyncio.create_task(heartbeat())
        try:
            t0 = time.monotonic()
            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, azctl.QuitScreen)
            await pilot.press("enter")
            for _ in range(200):
                await pilot.pause()
                if not app.is_running:
                    break
                await asyncio.sleep(0.02)
            elapsed = time.monotonic() - t0
        finally:
            hb.cancel()
        assert app.is_running is False
        assert ("shutdown",) in manager.calls
        assert elapsed >= 0.45, "the fake shutdown should have taken its full ~0.5s"
        # A blocked event loop could only have advanced the heartbeat a
        # handful of times regardless of wall-clock elapsed time; a live one
        # advances roughly once per 20ms throughout.
        assert len(ticks) >= (elapsed / 0.02) * 0.4, (len(ticks), elapsed)


# Finding: ServiceManager.refresh() does real socket.create_connection()
# calls for every configured port, synchronously on the main thread, every
# ~0.3s -- against a slow-to-refuse host this stalls the whole UI. Drive a
# (fake, but deliberately slow) refresh() through the normal tick path and
# assert the event loop stays responsive while it's in flight.
async def test_periodic_refresh_runs_off_the_event_loop():
    class SlowRefreshManager(FakeManager):
        def refresh(self):
            time.sleep(0.3)
            return []

    manager = SlowRefreshManager()
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        ticks = []

        async def heartbeat():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.02)

        hb = asyncio.create_task(heartbeat())
        try:
            t0 = time.monotonic()
            await pilot.pause(0.9)
            elapsed = time.monotonic() - t0
        finally:
            hb.cancel()
        assert len(ticks) >= (elapsed / 0.02) * 0.4, (len(ticks), elapsed)


# Finding: the signal.signal()-registered handler called
# self.call_from_thread(self._shutdown_from_signal) -- but a real OS signal
# handler always runs on the SAME thread that owns the asyncio loop, and
# Textual's call_from_thread unconditionally raises RuntimeError in that
# case, so no external SIGINT/SIGTERM/SIGHUP ever actually reached
# _do_shutdown(). _install_os_signal_handlers now uses
# loop.add_signal_handler, whose callback runs as an ordinary loop callback
# (no thread hop) -- calling _shutdown_from_signal directly here reproduces
# that exact calling convention and must not raise.
async def test_shutdown_from_signal_does_not_raise_and_shuts_down_cleanly():
    manager = FakeManager(owned={"blob"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        app._shutdown_from_signal()  # must not raise
        for _ in range(100):
            await pilot.pause()
            if not app.is_running:
                break
            await asyncio.sleep(0.02)
        assert ("shutdown",) in manager.calls
        assert app.exit_note == "Interrupted — stopped all services."


# Finding: a second signal arriving while the first shutdown's
# manager.shutdown() was still blocking used to raise a SECOND RuntimeError
# that aborted ServiceManager.shutdown()'s loop before every service got
# stopped, orphaning whichever hadn't been reached yet. The _shutdown_done
# guard must make a second call a harmless no-op instead.
async def test_second_signal_during_shutdown_is_a_harmless_no_op():
    manager = FakeManager(owned={"blob", "queue", "table"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        app._shutdown_from_signal()
        app._shutdown_from_signal()  # simulates a second SIGTERM mid-shutdown
        for _ in range(100):
            await pilot.pause()
            if not app.is_running:
                break
            await asyncio.sleep(0.02)
        assert manager.calls.count(("shutdown",)) == 1


async def _wait_until(predicate, pilot, attempts=30):
    """Drain the Pilot's message queue with zero-delay pause()s (NOT
    asyncio.sleep -- these cost negligible real wall-clock time) until
    `predicate()` is true. Used to observe the resize-redraw chain (App ->
    Screen -> our _redraw_after_resize, each a separate hop through
    call_after_refresh) settle without ever advancing far enough in real
    time for AzctlApp's own 0.1s periodic tick to fire and mask the result --
    i.e. this proves the *resize hook* redrew, not the ambient tick."""
    for _ in range(attempts):
        if predicate():
            return True
        await pilot.pause()
    return False


# Finding: only the periodic ~0.1s tick re-rendered the log/table/header
# panels, so a resized terminal briefly showed content computed for the OLD
# size -- render_logs() slicing far more (or fewer) lines than the new area
# can actually show, until the next tick caught up. Resizing must trigger its
# own redraw well before the next tick, not rely on it.
async def test_resize_redraws_the_log_panel_at_the_new_size_before_the_next_tick():
    manager = FakeManager()
    for i in range(40):
        manager.logs.append("blob", "line %d" % i)
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.resize_terminal(100, 40)
        assert await _wait_until(lambda: len(logpanel_text(app).splitlines()) > 10, pilot)
        tall_lines = len(logpanel_text(app).splitlines())

        await pilot.resize_terminal(100, 10)
        assert await _wait_until(lambda: len(logpanel_text(app).splitlines()) < tall_lines, pilot)
        short_lines = len(logpanel_text(app).splitlines())
        assert short_lines < tall_lines


# Finding: an unrelated ->broken transition's bell + red message used to fire
# underneath an open ConfirmScreen/QuitScreen/HelpScreen/ConnScreen (they only
# dim the base screen, they don't hide it), competing with the isolated
# decision the user is supposed to be focused on. It must be buffered while a
# modal is open and only surface once that modal is dismissed.
async def test_modal_suppresses_bell_and_message_then_flushes_on_dismiss():
    class DelayedBrokenManager(FakeManager):
        def __init__(self, owned=()):
            FakeManager.__init__(self, owned=owned)
            self.armed = False

        def refresh(self):
            if self.armed:
                self.armed = False
                return [azctl.Transition("queue", azctl.STARTING, azctl.BROKEN)]
            return []

        def broken_reason(self, _name):
            return "queue died"

    manager = DelayedBrokenManager(owned={"blob"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    bells = []
    app.bell = lambda: bells.append(True)
    async with dashboard(app) as pilot:
        await pilot.pause()
        # HelpScreen: a read-only overlay, so nothing else competes for the
        # message line after it's dismissed -- proves the flushed alert
        # actually surfaces, not just that the bell rings.
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, azctl.HelpScreen)
        manager.armed = True
        await pilot.pause(0.5)  # let the tick-gate fire the ->broken transition
        assert bells == [], "bell must not fire while a modal is open"
        assert app.ui.message is None or app.ui.message[1] != "red"
        assert app._pending_bell is True
        assert app._buffered_alert_message == ("queue died", "red")

        await pilot.press("x")  # any key closes the help overlay
        await pilot.pause()
        assert not isinstance(app.screen, azctl.HelpScreen)
        assert bells == [True], "bell must fire once the modal is dismissed"
        assert app.ui.message[0] == "queue died"
        assert app.ui.message[1] == "red"
