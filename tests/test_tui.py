"""Headless Textual dashboard tests for the rebuilt AzctlApp.

Everything runs without a TTY (Textual's run_test() is headless), without
azurite, and without launching a real interactive event loop: AzctlApp is
driven through ``async with app.run_test() as pilot``, wired either to a
FakeManager (fast, deterministic) or a real ServiceManager pointed at a fake
``python3 -c`` TCP-listener command injected through the ``command_for``
seam (helpers.listener_command) -- never a real Azurite install.

Coverage is mapped to BEHAVIOR.md: the five-state cards, selection/log-panel
following (keyboard and mouse), action-highlight styling and destructive
colours, Start/Stop/Restart/Save/Free-port/Stop-all semantics and their
confirmations, the merged-log and timestamp toggles, the log filter bar,
connection-string copy (OSC 52), the help overlay, the three-way quit
question, the broken-transition bell (and its buffering under a modal),
non-TTY refusal, and the off-loop threading guarantees.
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

import azctl
from helpers import free_port, listener_command, spawn_listener, wait_busy_clears, wait_for


@asynccontextmanager
async def dashboard(app):
    """Run `app` headlessly via Pilot, then force a clean `app.exit()`
    *before* letting `run_test()`'s own teardown proceed."""
    async with app.run_test() as pilot:
        try:
            yield pilot
        finally:
            if app.is_running:
                app.exit()
            await pilot.pause()


def export(renderable, width=140):
    console = Console(record=True, width=width, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def view(name, state, port, pid=None, uptime=None, ever=False, exit_code=None):
    return azctl.ServiceView(name, state, port, pid, uptime, ever, exit_code)


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
    """Replays a fixed list of Transitions, one per refresh() call."""

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


# --- render/export helpers -------------------------------------------------


def card_text(app, name, width=60):
    card = app.query_one("#card-%s" % name)
    return export(card.content, width=width)


def footer_text(app, width=140):
    return export(app.query_one("#footer").content, width=width)


def button_texts(app):
    out = []
    for widget in app.query(azctl.ActionButton):
        text = widget.content
        spans = [
            (str(s.style), text.plain[s.start : s.end]) for s in text.spans
        ]
        out.append((text.plain, spans))
    return out


def hot_index(app):
    # The hot button is rendered tight-bracketed ("[Stop]"), cold ones are
    # padded ("[ Stop ]") -- see btn_label().
    for i, (plain, _spans) in enumerate(button_texts(app)):
        if re.fullmatch(r"\[[^\]\s][^\]]*\]", plain):
            return i
    raise AssertionError("no highlighted button found")


def logview_text(app):
    lv = app.query_one("#logview")
    return "\n".join(strip.text for strip in lv.lines)


# --- five-state cards -------------------------------------------------------


async def test_five_states_symbols_and_colours_in_cards():
    views_a = [
        view("blob", azctl.RUNNING, 10000, pid=4242, uptime=3725.0, ever=True),
        view("queue", azctl.STARTING, 10001, pid=4243, uptime=2.0, ever=True),
        view("table", azctl.STOPPED, 10002),
    ]
    app = azctl.AzctlApp(azctl.Config(), FakeManager(views=views_a))
    async with dashboard(app) as pilot:
        await pilot.pause()
        assert "● running" in card_text(app, "blob")
        assert "◐ starting" in card_text(app, "queue")
        assert "○ stopped" in card_text(app, "table")
        assert app.query_one("#card-blob").has_class("state-running")
        assert app.query_one("#card-queue").has_class("state-starting")
        assert app.query_one("#card-table").has_class("state-stopped")
        # port / PID / uptime formatting
        blob_out = card_text(app, "blob")
        assert "10000" in blob_out
        assert "4242" in blob_out
        assert "1:02:05" in blob_out
        table_out = card_text(app, "table")
        assert "pid —" in table_out
        assert "up" not in table_out

    views_b = [
        view("blob", azctl.BROKEN, 10000, ever=True, exit_code=3),
        view("queue", azctl.PORT_IN_USE, 10001),
        view("table", azctl.STOPPED, 10002),
    ]
    app2 = azctl.AzctlApp(azctl.Config(), FakeManager(views=views_b))
    async with dashboard(app2) as pilot:
        await pilot.pause()
        assert "✖ broken" in card_text(app2, "blob")
        assert app2.query_one("#card-blob").has_class("state-broken")
        assert "◆ port in use" in card_text(app2, "queue")
        assert app2.query_one("#card-queue").has_class("state-port_in_use")
        # a broken death shows its exit code on the card
        assert "exit 3" in card_text(app2, "blob")


async def test_selected_card_is_marked_and_follows_the_keyboard():
    app = azctl.AzctlApp(azctl.Config(), FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        assert app.ui.selected == 0
        assert app.query_one("#card-blob").has_class("selected")
        assert "▸" in card_text(app, "blob")
        await pilot.press("down")
        await pilot.pause()
        assert app.ui.selected == 1
        assert app.query_one("#card-queue").has_class("selected")
        assert not app.query_one("#card-blob").has_class("selected")
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("down")  # wraps back to 0
        await pilot.pause()
        assert app.ui.selected == 0
        assert app.query_one("#card-blob").has_class("selected")


# --- up/down selection + the log panel following it -------------------------


async def test_up_down_selects_service_and_log_panel_follows():
    manager = FakeManager()
    manager.logs.append("blob", "hello from blob")
    manager.logs.append("queue", "hello from queue")
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        assert "hello from blob" in logview_text(app)
        await pilot.press("down")
        await pilot.pause()
        assert app.ui.selected == 1
        assert app.ui.message[0] == "Selected Queue."
        assert app.ui.message[1] == "grey"
        assert "hello from queue" in logview_text(app)
        assert "hello from blob" not in logview_text(app)
        assert "logs · Queue" in app.query_one("#logview").border_title
        await pilot.press("up")
        await pilot.pause()
        assert app.ui.selected == 0
        assert app.ui.message[0] == "Selected Blob."


async def test_log_panel_says_never_run_for_an_untouched_service():
    manager = FakeManager()
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        out = logview_text(app)
        assert "Blob has never run. Press Enter on [Start] to launch it." in out


async def test_placeholder_clears_when_real_output_arrives():
    manager = FakeManager()
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        assert "never run" in logview_text(app)
        manager.logs.append("blob", "first real line")
        await pilot.press("down")
        await pilot.press("up")  # force a reset+refeed of Blob's view
        await pilot.pause()
        out = logview_text(app)
        assert "never run" not in out
        assert "first real line" in out


# --- left/right action highlight + destructive styling ----------------------


async def test_left_right_moves_button_highlight():
    app = azctl.AzctlApp(azctl.Config(), FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        assert app.ui.button == 0
        assert hot_index(app) == 0
        await pilot.press("right")
        await pilot.pause()
        assert app.ui.button == 1
        assert hot_index(app) == 1
        await pilot.press("left")
        await pilot.pause()
        assert app.ui.button == 0
        # Clamped at the left edge.
        await pilot.press("left")
        await pilot.pause()
        assert app.ui.button == 0
        # Clamped at the right edge.
        for _ in range(10):
            await pilot.press("right")
            await pilot.pause()
        assert app.ui.button == len(azctl.BUTTONS) - 1
        assert hot_index(app) == len(azctl.BUTTONS) - 1


async def test_hot_button_style_follows_the_highlight():
    app = azctl.AzctlApp(azctl.Config(), FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        # Button 0 (Start, safe) is hot: its label is drawn reversed.
        start_spans = button_texts(app)[0][1]
        assert any("reverse" in str(style) for style, _text in start_spans)
        # A cold dangerous button (Stop) is red but never reversed.
        stop_spans = button_texts(app)[1][1]
        assert any("red" in str(style) for style, _text in stop_spans)
        assert not any("reverse" in str(style) for style, _text in stop_spans)
        # Move the highlight onto Stop: now it is reversed too.
        await pilot.press("right")
        await pilot.pause()
        stop_spans = button_texts(app)[1][1]
        assert any("reverse" in str(style) and "bold" in str(style) for style, _text in stop_spans)


# --- Enter on Start: no confirm, real fake service --------------------------


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
    command instead of a mock -- no azurite binary anywhere, but a genuine
    child process and a genuine port."""
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
            # the card eventually reflects it too
            for _ in range(30):
                await pilot.pause(0.1)
                if "● running" in card_text(app, "blob"):
                    break
            assert "● running" in card_text(app, "blob")
    finally:
        manager.shutdown()


# --- Stop/Restart: confirm modal, Enter confirms, other key cancels ---------


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
        assert app.ui.message[0] == "Stopped Blob."
        assert app.ui.message[1] == "green"
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


# --- Free port: names the squatter, and the no-op path -----------------------


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


# --- Stop all: confirm, grey message when nothing running --------------------


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


# --- Save: reports via the app message ---------------------------------------


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


async def test_save_all_writes_the_merged_log_via_S(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = azctl.Config(blob_port=free_port(), queue_port=free_port(), table_port=free_port())
    manager = azctl.ServiceManager(config, command_for=lambda _n, _c: None)
    manager.logs.append("blob", "b-line")
    manager.logs.append("queue", "q-line")
    app = azctl.AzctlApp(config, manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("S")
        await pilot.pause()
        path = os.path.join(str(tmp_path), "azurite-all.log")
        assert os.path.exists(path)
        content = open(path).read()
        assert "[blob] b-line" in content
        assert "[queue] q-line" in content
        assert "azurite-all.log" in app.ui.message[0]


# --- 'a' merged view: arrival order, colour-tagged, footer shows mode --------


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
        await pilot.pause()
        assert app.ui.combined_logs is True
        assert "logs: ALL" in footer_text(app)
        assert "all services (merged)" in app.query_one("#logview").border_title
        out = logview_text(app)
        assert "[blob] blob-first" in out
        assert "[queue] queue-second" in out
        assert "[table] table-fourth" in out
        positions = [out.index(t) for t in ("blob-first", "queue-second", "blob-third", "table-fourth")]
        assert positions == sorted(positions)
        await pilot.press("a")  # toggle back off
        await pilot.pause()
        assert app.ui.combined_logs is False
        assert "logs: selected" in footer_text(app)
        # back to the selected-service view: only Blob's own lines remain
        out = logview_text(app)
        assert "blob-first" in out
        assert "queue-second" not in out
        assert "table-fourth" not in out


# --- 't' timestamp toggle -----------------------------------------------------


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
        assert stamp not in logview_text(app)
        await pilot.press("t")
        await pilot.pause()
        assert app.ui.timestamps is True
        assert app.ui.message[0] == "Timestamps on."
        assert "%s hello world" % stamp in logview_text(app)
        await pilot.press("t")
        await pilot.pause()
        assert app.ui.message[0] == "Timestamps off."
        assert stamp not in logview_text(app)


# --- '/' filter bar ------------------------------------------------------------


async def test_filter_bar_narrows_lines_live_and_esc_clears_it():
    manager = FakeManager()
    manager.logs.append("blob", "alpha one")
    manager.logs.append("blob", "beta two")
    manager.logs.append("blob", "alpha three")
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        flt = app.query_one("#logfilter")
        assert not flt.has_class("visible")
        await pilot.press("/")
        await pilot.pause()
        assert flt.has_class("visible")
        assert app.focused is flt
        for ch in "alpha":
            await pilot.press(ch)
        await pilot.pause()
        assert app.ui.filter_text == "alpha"
        assert "/alpha/" in app.query_one("#logview").border_subtitle
        out = logview_text(app)
        assert "alpha one" in out
        assert "alpha three" in out
        assert "beta two" not in out
        await pilot.press("escape")
        await pilot.pause()
        assert app.ui.filter_text == ""
        assert not flt.has_class("visible")
        out = logview_text(app)
        assert "beta two" in out


async def test_filter_survives_until_cleared_and_indicator_shows_in_footer():
    manager = FakeManager()
    manager.logs.append("blob", "keep me")
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("/", "k", "e", "e")
        await pilot.pause()
        assert "/kee/" in footer_text(app)
        await pilot.press("enter")  # Enter keeps the filter, closes the bar's focus
        await pilot.pause()
        assert app.ui.filter_text == "kee"


# --- mouse: click a card to select it -----------------------------------------


async def test_mouse_click_on_a_card_selects_it_in_one_click():
    manager = FakeManager()
    manager.logs.append("table", "table output here")
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        assert app.ui.selected == 0
        await pilot.click("#card-table")
        await pilot.pause()
        assert app.ui.selected == 2
        assert app.ui.message[0] == "Selected Table."
        assert "table output here" in logview_text(app)


async def test_selection_survives_repeated_refresh_ticks_after_a_click():
    app = azctl.AzctlApp(azctl.Config(), FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.click("#card-table")
        await pilot.pause()
        assert app.ui.selected == 2
        await pilot.pause(0.9)  # several 0.1s ticks, well past the 3rd-tick refresh gate
        assert app.ui.selected == 2
        assert app.query_one("#card-table").has_class("selected")


# --- mouse: click an action button to run it -----------------------------------


async def test_clicking_start_runs_it_without_confirm():
    manager = FakeManager()
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        buttons = list(app.query(azctl.ActionButton))
        await pilot.click(buttons[0])
        await pilot.pause()
        assert ("start", "blob") in manager.calls
        assert app.ui.button == 0


# --- 'c' connection string copy (OSC 52) + overlay -----------------------------


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


# --- '?' help overlay lists every key ------------------------------------------


async def test_help_overlay_lists_every_key_and_any_key_closes_it():
    app = azctl.AzctlApp(azctl.Config(), FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, azctl.HelpScreen)
        out = export(app.screen.query_one(azctl._ModalBody).content, width=140)
        for needle in (
            "↑ / ↓", "← / →", "Enter", "a", "t", "/", "c", "S", "?", "Esc", "q", "mouse",
            "Start", "Stop", "Restart", "Save", "Free port", "Start all", "Stop all",
        ):
            assert needle in out, needle
        await pilot.press("x")
        await pilot.pause()
        assert not isinstance(app.screen, azctl.HelpScreen)


async def test_help_overlay_mentions_ctrl_c_and_ctrl_q():
    app = azctl.AzctlApp(azctl.Config(), FakeManager())
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        out = export(app.screen.query_one(azctl._ModalBody).content, width=140)
        assert "Ctrl+C" in out
        assert "Ctrl+Q" in out


# --- three-way quit modal --------------------------------------------------------


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
        for other_key in ("x", "a", "up", "left"):
            await pilot.press(other_key)
            await pilot.pause()
            assert isinstance(app.screen, azctl.QuitScreen), other_key
            assert app.is_running is True, other_key
        assert ("shutdown",) not in manager.calls
        assert ("detach_all",) not in manager.calls


async def test_quit_dialog_offers_three_choices_as_clickable_buttons():
    manager = FakeManager(owned={"blob"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        actions = [w.btn.action for w in app.screen.query(azctl.ActionButton)]
        assert actions == ["stop", "detach", "stay"]


# --- bell on a ->broken transition ------------------------------------------------


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


# --- non-TTY refusal: exit 2, no App ever constructed ------------------------------


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


# --- regression: AzctlApp's refresh timer must not race Pilot teardown -------------


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


# --- ctrl+c / ctrl+q are aliases of q -----------------------------------------------


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


# --- confirm modal docks at the footer where the contract says it belongs -----------


async def test_confirm_modal_docks_in_the_footer_region():
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
        # The prompt sits in the footer band (the footer carries a horizontal
        # margin, so a one-row offset is still "in the footer").
        assert abs(body.region.y - footer_y) <= 2
        await pilot.press("escape")
        await pilot.pause()


# --- shutdown keeps the event loop responsive ----------------------------------------


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
        assert len(ticks) >= (elapsed / 0.02) * 0.4, (len(ticks), elapsed)


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


# --- signals ---------------------------------------------------------------------------


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


# --- resize redraws promptly -------------------------------------------------------------


async def test_resize_redraws_the_log_area_at_the_new_size_before_the_next_tick():
    manager = FakeManager()
    for i in range(40):
        manager.logs.append("blob", "line %d" % i)
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        lv = app.query_one("#logview")

        async def wait_region_height(pred, attempts=40):
            for _ in range(attempts):
                if pred(lv.region.height):
                    return True
                await pilot.pause()
            return False

        tall = lv.region.height
        assert tall > 0
        await pilot.resize_terminal(100, 12)
        assert await wait_region_height(lambda h: 0 < h < tall)
        short = lv.region.height
        await pilot.resize_terminal(100, 44)
        assert await wait_region_height(lambda h: h > short)


# --- buffered alerts flush when a modal dismisses ------------------------------------------


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
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, azctl.HelpScreen)
        manager.armed = True
        await pilot.pause(0.5)
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


# --- sparkline activity buckets --------------------------------------------------------------


async def test_cards_render_an_activity_sparkline_after_traffic():
    manager = FakeManager()
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        for i in range(5):
            manager.logs.append("queue", "tick %d" % i)
        await pilot.pause(1.5)  # let several refresh ticks bucket the traffic
        out = card_text(app, "queue", width=80)
        assert any(ch in out for ch in azctl.SPARK_BLOCKS), (
            "expected sparkline blocks on the card after fresh traffic"
        )


# --- busy spinner ------------------------------------------------------------------------------


async def test_busy_state_shows_a_spinner_in_the_footer():
    class SlowStopManager(FakeManager):
        def stop(self, name):
            time.sleep(0.6)
            return FakeManager.stop(self, name)

    manager = SlowStopManager(owned={"blob"})
    app = azctl.AzctlApp(azctl.Config(), manager)
    async with dashboard(app) as pilot:
        await pilot.pause()
        await pilot.press("right")  # Stop
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")  # confirm
        await pilot.pause()
        saw_spinner = False
        for _ in range(20):
            if "working…" in footer_text(app) or "…" in footer_text(app):
                saw_spinner = True
                break
            await pilot.pause()
        assert await wait_busy_clears(pilot, app)
        assert saw_spinner, "footer never showed the busy indicator while stopping"
