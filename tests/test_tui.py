"""Headless TUI render + quality-of-life tests.

Everything runs without a TTY, without azurite, and without ever launching
the interactive event loop: pure render functions are drawn onto recorded
consoles, and the Dashboard controller is driven through handle_event().
The one run()-loop test (terminal bell) uses a fake input reader, a fake
manager, and a StringIO console, and exits after two bounded iterations.
"""

import base64
import io
import os
import queue
import re
import signal
import sys
import threading
import time

import pytest
from rich.console import Console

import azctl
from helpers import free_port, spawn_listener, wait_for


# --- render helpers -----------------------------------------------------


def export(renderable, width=100):
    console = Console(record=True, width=width)
    console.print(renderable)
    return console.export_text()


def segments(renderable, width=100):
    console = Console(file=io.StringIO(), width=width)
    return list(console.render(renderable))


def style_of(renderable, needle, width=100):
    """The rich Style of the first rendered segment containing `needle`."""
    for seg in segments(renderable, width=width):
        if seg.text and needle in seg.text:
            return seg.style
    raise AssertionError("no rendered segment contains %r" % needle)


def colour_name(style):
    assert style is not None and style.color is not None
    return style.color.name


def view(name, state, port, pid=None, uptime=None, ever=False, exit_code=None):
    return azctl.ServiceView(name, state, port, pid, uptime, ever, exit_code)


def log_line(seq, service, text, wall=None):
    return azctl.LogLine(seq, float(seq), wall if wall is not None else time.time(), service, text)


# --- controller fakes ---------------------------------------------------


class FakeManager:
    """Just enough ServiceManager surface for Dashboard.handle_event()."""

    def __init__(self, owned=()):
        self.logs = azctl.LogStore()
        self.owned = set(owned)
        self.calls = []

    def owns_live(self, name):
        return name in self.owned

    def any_owned(self):
        return bool(self.owned)

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

    def refresh(self):
        return []

    def views(self):
        return [
            view(name, azctl.STOPPED, 10000 + i)
            for i, name in enumerate(azctl.SERVICE_ORDER)
        ]

    def save_service_log(self, name, directory=None):
        self.calls.append(("save", name))
        return True, "Wrote 0 lines to azurite-%s.log (log was empty)" % name, "yellow"

    def save_merged_log(self, directory=None):
        self.calls.append(("save_merged",))
        return True, "Wrote 0 lines to azurite-all.log (log was empty)", "yellow"

    def notice_external_kill(self, name):
        self.calls.append(("notice_external_kill", name))

    def broken_reason(self, name):
        return None


def key(name):
    return azctl.KeyEvent(name)


def make_dashboard(manager=None, clock=None, **kwargs):
    config = kwargs.pop("config", azctl.Config())
    manager = manager if manager is not None else FakeManager()
    if clock is not None:
        kwargs["clock"] = clock
    return azctl.Dashboard(config, manager, **kwargs)


# --- the status table ---------------------------------------------------

FIVE_STATE_VIEWS = [
    view("blob", azctl.RUNNING, 10000, pid=4242, uptime=3725.0, ever=True),
    view("queue", azctl.STARTING, 10001, pid=4243, uptime=2.0, ever=True),
    view("table", azctl.STOPPED, 10002),
    view("blob", azctl.BROKEN, 10000, ever=True, exit_code=3),
    view("queue", azctl.PORT_IN_USE, 10001),
]


def test_table_shows_all_five_states_with_symbols():
    out = export(azctl.render_table(FIVE_STATE_VIEWS, selected=0))
    assert "● running" in out
    assert "◐ starting" in out
    assert "○ stopped" in out
    assert "✖ broken" in out
    assert "◆ port in use" in out


def test_table_state_colours():
    table = azctl.render_table(FIVE_STATE_VIEWS, selected=0)
    assert colour_name(style_of(table, "● running")) == "green"
    assert colour_name(style_of(table, "◐ starting")) == "yellow"
    assert colour_name(style_of(table, "○ stopped")) == "grey50"
    assert colour_name(style_of(table, "✖ broken")) == "red"
    assert colour_name(style_of(table, "◆ port in use")) == "magenta"


def test_table_selected_marker_and_columns():
    views = [
        view("blob", azctl.RUNNING, 10000, pid=4242, uptime=3725.0, ever=True),
        view("queue", azctl.STOPPED, 10001),
        view("table", azctl.STOPPED, 10002),
    ]
    out = export(azctl.render_table(views, selected=1))
    assert "▸ Queue" in out
    assert "▸ Blob" not in out and "▸ Table" not in out
    # Column headers, exactly the five from BEHAVIOR.md.
    for header in ("Service", "Status", "Port", "PID", "Uptime"):
        assert header in out
    # PID/uptime dashes when nothing runs; uptime is h:mm:ss.
    assert "—" in out
    assert "1:02:05" in out
    assert "4242" in out


def test_table_marker_moves_with_selection():
    views = FIVE_STATE_VIEWS[:3]
    for selected, name in enumerate(("Blob", "Queue", "Table")):
        out = export(azctl.render_table(views, selected=selected))
        assert "▸ %s" % name in out


def test_format_uptime_hours_minutes_seconds():
    assert azctl.format_uptime(0) == "0:00:00"
    assert azctl.format_uptime(59) == "0:00:59"
    assert azctl.format_uptime(3725.9) == "1:02:05"
    assert azctl.format_uptime(36000) == "10:00:00"


# --- the log panel ------------------------------------------------------


def test_log_panel_truncates_long_lines_without_wrapping():
    long_text = "x" * 200
    lines = [log_line(0, "blob", long_text)]
    panel = azctl.render_logs(
        lines, combined=False, timestamps=False, service="blob", ever_started=True, height=5
    )
    out = export(panel, width=40)
    rendered = [row for row in out.splitlines() if row.strip()]
    # borders + exactly one content row: the long line never wraps.
    assert len(rendered) == 3
    assert "…" in out
    assert long_text not in out


def test_log_panel_newest_last():
    lines = [log_line(i, "blob", "line-%02d" % i) for i in range(50)]
    panel = azctl.render_logs(
        lines, combined=False, timestamps=False, service="blob", ever_started=True, height=10
    )
    out = export(panel)
    # height 10 → 8 inner rows → lines 42..49 only, newest at the bottom.
    assert "line-41" not in out
    assert "line-42" in out and "line-49" in out
    assert out.index("line-48") < out.index("line-49")


def test_log_panel_never_run_hint():
    panel = azctl.render_logs(
        [], combined=False, timestamps=False, service="blob", ever_started=False, height=10
    )
    out = export(panel)
    assert "Blob has never run." in out
    assert "[Start]" in out  # tells the user what to press


def test_log_panel_empty_after_running():
    panel = azctl.render_logs(
        [], combined=False, timestamps=False, service="queue", ever_started=True, height=10
    )
    out = export(panel)
    assert "no output yet" in out
    assert "has never run" not in out


def test_log_panel_title_names_selected_service():
    panel = azctl.render_logs(
        [], combined=False, timestamps=False, service="table", ever_started=False, height=6
    )
    assert "logs · Table" in export(panel)


# --- the merged view ----------------------------------------------------


def merged_lines():
    store = azctl.LogStore()
    store.append("blob", "blob-first")
    store.append("queue", "queue-second")
    store.append("blob", "blob-third")
    store.append("table", "table-fourth")
    return store.merged()


def test_merged_view_tags_every_line_with_its_service():
    panel = azctl.render_logs(
        merged_lines(), combined=True, timestamps=False, service="blob", ever_started=True, height=10
    )
    out = export(panel)
    assert "[blob] blob-first" in out
    assert "[queue] queue-second" in out
    assert "[table] table-fourth" in out
    assert "logs · all services (merged)" in out


def test_merged_view_service_colours():
    panel = azctl.render_logs(
        merged_lines(), combined=True, timestamps=False, service="blob", ever_started=True, height=10
    )
    assert colour_name(style_of(panel, "[blob]")) == "cyan"
    assert colour_name(style_of(panel, "[queue]")) == "green"
    assert colour_name(style_of(panel, "[table]")) == "magenta"
    # The line body is coloured too, not just the tag.
    assert colour_name(style_of(panel, "blob-first")) == "cyan"


def test_merged_view_arrival_order_not_grouped():
    panel = azctl.render_logs(
        merged_lines(), combined=True, timestamps=False, service="blob", ever_started=True, height=10
    )
    out = export(panel)
    positions = [out.index(t) for t in ("blob-first", "queue-second", "blob-third", "table-fourth")]
    assert positions == sorted(positions)


# --- the button bar -----------------------------------------------------


def bar_text(active=0):
    return "".join(seg.text for seg in segments(azctl.render_button_bar(active), width=120))


def test_button_bar_order_and_group_separator():
    text = bar_text()
    order = ["[Start]", "[Stop]", "[Restart]", "[Save]", "[Free port]", "[Start all]", "[Stop all]"]
    positions = [text.index(token) for token in order]
    assert positions == sorted(positions)
    # The all-services group is visually separated from the per-service five.
    assert "[Free port] │ [Start all]" in text
    assert "│" not in text.split("[Free port]")[0]


def test_button_bar_highlight_is_boxed_reverse():
    bar = azctl.render_button_bar(0)
    active = style_of(bar, "[Start]", width=120)
    assert active.reverse and active.bold
    inactive = style_of(bar, "[Save]", width=120)
    assert not (inactive and inactive.reverse)


def test_button_bar_destructive_buttons_red_before_highlight():
    bar = azctl.render_button_bar(0)  # Start highlighted, everything else idle
    for label in ("[Stop]", "[Restart]", "[Free port]", "[Stop all]"):
        assert colour_name(style_of(bar, label, width=120)) == "red", label
    for label in ("[Save]", "[Start all]"):
        style = style_of(bar, label, width=120)
        assert style is None or style.color is None, label


def test_button_bar_active_destructive_keeps_red():
    bar = azctl.render_button_bar(1)  # Stop highlighted
    style = style_of(bar, "[Stop]", width=120)
    assert style.reverse and style.bold
    assert colour_name(style) == "red"


# --- confirmation prompts ----------------------------------------------


def test_stop_asks_with_plain_named_prompt():
    dash = make_dashboard(FakeManager(owned={"blob"}))
    dash._activate(1)  # Stop
    assert dash.ui.mode == "confirm"
    assert dash.ui.pending_prompt == "Stop Blob?"


def test_confirm_prompt_names_the_selected_service():
    dash = make_dashboard(FakeManager(owned={"queue"}))
    dash.handle_event(key("down"))
    dash._activate(1)
    assert dash.ui.pending_prompt == "Stop Queue?"


def test_restart_and_stop_all_prompts():
    dash = make_dashboard(FakeManager(owned={"blob"}))
    dash._activate(2)  # Restart
    assert dash.ui.pending_prompt == "Restart Blob?"
    dash.handle_event(key("esc"))
    dash._activate(6)  # Stop all
    assert dash.ui.pending_prompt == "Stop all services?"


def test_confirm_enter_runs_the_action():
    manager = FakeManager(owned={"blob"})
    dash = make_dashboard(manager)
    dash._activate(1)
    dash.handle_event(key("enter"))
    assert ("stop", "blob") in manager.calls
    assert dash.ui.mode == "normal"
    assert dash.ui.message[0] == "Stopped Blob."


def test_confirm_esc_cancels_with_message():
    manager = FakeManager(owned={"blob"})
    dash = make_dashboard(manager)
    dash._activate(1)
    dash.handle_event(key("esc"))
    assert ("stop", "blob") not in manager.calls
    assert dash.ui.message[0] == "Cancelled."
    assert dash.ui.message[1] == "grey"
    assert dash.ui.mode == "normal"


def test_confirm_any_other_key_cancels():
    for other in ("z", "q", "a", "up", "left", " "):
        manager = FakeManager(owned={"blob"})
        dash = make_dashboard(manager)
        dash._activate(1)
        dash.handle_event(key(other))
        assert ("stop", "blob") not in manager.calls, other
        assert dash.ui.message[0] == "Cancelled.", other
        assert dash.ui.mode == "normal", other


def test_confirm_prompt_rendered_in_footer():
    dash = make_dashboard(FakeManager(owned={"blob"}))
    dash._activate(1)
    out = export(azctl.render_footer(dash.ui, now=time.time()), width=120)
    assert "Stop Blob?" in out
    assert "Enter: yes" in out
    assert "cancel" in out


def test_stop_when_not_running_does_not_ask():
    dash = make_dashboard(FakeManager())
    dash._activate(1)
    assert dash.ui.mode == "normal"
    assert "not running" in dash.ui.message[0]
    assert dash.ui.message[1] == "grey"


# --- free port: names the squatter --------------------------------------


def test_free_port_confirmation_names_process_pid_and_port():
    port = free_port()
    squatter = spawn_listener(port)
    try:
        config = azctl.Config(blob_port=port, queue_port=free_port(), table_port=free_port())
        dash = make_dashboard(FakeManager(), config=config)
        dash._activate(4)  # Free port
        assert dash.ui.mode == "confirm"
        match = re.fullmatch(r"Kill (.+) \(PID (\d+)\) on port (\d+)\?", dash.ui.pending_prompt)
        assert match, dash.ui.pending_prompt
        assert int(match.group(2)) == squatter.pid
        assert int(match.group(3)) == port
        # Cancelling leaves the squatter alone.
        dash.handle_event(key("x"))
        assert dash.ui.message[0] == "Cancelled."
        assert squatter.poll() is None
    finally:
        squatter.terminate()
        squatter.wait(timeout=5)


def test_free_port_confirm_enter_kills_and_reports_green():
    port = free_port()
    squatter = spawn_listener(port)
    try:
        config = azctl.Config(blob_port=port, queue_port=free_port(), table_port=free_port())
        dash = make_dashboard(FakeManager(), config=config, sleep=lambda _s: None)
        dash._activate(4)
        assert dash.ui.mode == "confirm"
        dash.handle_event(key("enter"))
        assert dash.ui.message[1] == "green"
        assert "free" in dash.ui.message[0]
        assert str(squatter.pid) in dash.ui.message[0]
        assert wait_for(lambda: squatter.poll() is not None)
    finally:
        if squatter.poll() is None:
            squatter.terminate()
            squatter.wait(timeout=5)


def test_free_port_with_nothing_listening_asks_nothing():
    config = azctl.Config(blob_port=free_port(), queue_port=free_port(), table_port=free_port())
    dash = make_dashboard(FakeManager(), config=config)
    dash._activate(4)
    assert dash.ui.mode == "normal"
    assert dash.ui.message[0] == "Nothing is listening on port %d." % config.blob_port
    assert dash.ui.message[1] == "grey"


# --- messages: colour semantics + fade timing ---------------------------


def test_message_style_mapping_covers_the_four_semantics():
    assert azctl.MSG_STYLES["green"] == "green"
    assert "red" in azctl.MSG_STYLES["red"]
    assert azctl.MSG_STYLES["yellow"] == "yellow"
    assert azctl.MSG_STYLES["grey"] == "grey50"


def test_selection_change_is_a_grey_routine_note():
    dash = make_dashboard()
    dash.handle_event(key("down"))
    assert dash.ui.message[0] == "Selected Queue."
    assert dash.ui.message[1] == "grey"


def test_azurite_missing_start_is_red_and_names_install_command():
    config = azctl.Config(blob_port=free_port(), queue_port=free_port(), table_port=free_port())
    manager = azctl.ServiceManager(config, command_for=lambda _name, _cfg: None)
    dash = make_dashboard(manager, config=config)
    dash._activate(0)  # Start
    assert dash.ui.message[1] == "red"
    assert "npm install -g azurite" in dash.ui.message[0]


def test_empty_save_is_yellow_worked_with_caveat(tmp_path):
    config = azctl.Config(blob_port=free_port(), queue_port=free_port(), table_port=free_port())
    manager = azctl.ServiceManager(config, command_for=lambda _name, _cfg: None)
    ok, msg, style = manager.save_service_log("blob", directory=str(tmp_path))
    assert ok
    assert style == "yellow"
    assert "0 lines" in msg and "empty" in msg


def test_message_fades_after_ttl_via_clock_seam():
    now = [1000.0]
    dash = make_dashboard(clock=lambda: now[0])
    dash.show("saved fine", "green")
    text, style, expires = dash.ui.message
    assert (text, style) == ("saved fine", "green")
    assert expires == 1000.0 + azctl.MESSAGE_TTL
    # Just before expiry the footer shows it; just after, it is gone.
    visible = export(azctl.render_footer(dash.ui, now=expires - 0.1), width=120)
    assert "saved fine" in visible
    faded = export(azctl.render_footer(dash.ui, now=expires + 0.1), width=120)
    assert "saved fine" not in faded


def test_footer_message_rendered_with_semantic_colour():
    dash = make_dashboard(clock=lambda: 0.0)
    dash.show("boom", "red")
    footer = azctl.render_footer(dash.ui, now=1.0)
    style = style_of(footer, "boom", width=120)
    assert colour_name(style) == "red"


# --- help overlay -------------------------------------------------------


def test_help_overlay_lists_every_key_and_button():
    out = export(azctl.render_help(), width=110)
    for needle in (
        "↑ / ↓",
        "← / →",
        "Enter",
        "a",
        "t",
        "c",
        "S",
        "?",
        "Esc",
        "q",
        "mouse",
        "Start",
        "Stop",
        "Restart",
        "Save",
        "Free port",
        "Start all",
        "Stop all",
    ):
        assert needle in out, needle
    assert "press any key to close" in out


def test_question_mark_opens_help_and_any_key_closes():
    dash = make_dashboard()
    dash.handle_event(key("?"))
    assert dash.ui.mode == "help"
    dash.handle_event(key("z"))
    assert dash.ui.mode == "normal"


def test_footer_hints_present_and_combined_indicator():
    dash = make_dashboard()
    out = export(azctl.render_footer(dash.ui, now=time.time()), width=140)
    assert "? help" in out and "q quit" in out
    assert "selected" in out and "ALL" not in out
    dash.handle_event(key("a"))
    out = export(azctl.render_footer(dash.ui, now=time.time()), width=140)
    assert "ALL" in out


# --- connection strings + OSC 52 ---------------------------------------


def test_connection_string_uses_custom_ports_and_service_endpoint():
    config = azctl.Config(host="127.0.0.1", blob_port=12345, queue_port=23456, table_port=34567)
    blob = azctl.connection_string("blob", config)
    assert "DefaultEndpointsProtocol=http;" in blob
    assert "AccountName=devstoreaccount1;" in blob
    assert "AccountKey=%s;" % azctl.ACCOUNT_KEY in blob
    assert "BlobEndpoint=http://127.0.0.1:12345/devstoreaccount1;" in blob
    queue_cs = azctl.connection_string("queue", config)
    assert "QueueEndpoint=http://127.0.0.1:23456/devstoreaccount1;" in queue_cs
    table_cs = azctl.connection_string("table", config)
    assert "TableEndpoint=http://127.0.0.1:34567/devstoreaccount1;" in table_cs


def test_osc52_payload_is_valid_base64_of_the_text():
    text = azctl.connection_string("blob", azctl.Config(blob_port=19191))
    sink = io.StringIO()
    azctl.copy_osc52(text, stream=sink)
    raw = sink.getvalue()
    match = re.fullmatch(r"\x1b\]52;c;([A-Za-z0-9+/=]+)\x07", raw)
    assert match, repr(raw)
    payload = match.group(1)
    assert base64.b64decode(payload.encode("ascii"), validate=True).decode("utf-8") == text


def test_c_key_copies_selected_string_and_opens_overlay(monkeypatch):
    copied = []
    monkeypatch.setattr(azctl, "copy_osc52", lambda text: copied.append(text))
    config = azctl.Config(blob_port=free_port(), queue_port=free_port(), table_port=free_port())
    dash = make_dashboard(config=config)
    dash.handle_event(key("down"))  # select Queue
    dash.handle_event(key("c"))
    assert dash.ui.mode == "conn"
    assert copied == [azctl.connection_string("queue", config)]
    assert dash.ui.message[1] == "green"
    assert "Queue" in dash.ui.message[0]
    dash.handle_event(key("x"))  # any key closes
    assert dash.ui.mode == "normal"


def test_conn_overlay_lists_all_three_and_marks_selected():
    config = azctl.Config(blob_port=11111, queue_port=22222, table_port=33333)
    out = export(azctl.render_conn(config, selected=2), width=280)
    assert "▸ Table" in out
    assert "Blob" in out and "Queue" in out
    for port in ("11111", "22222", "33333"):
        assert port in out
    assert "OSC 52" in out


# --- timestamp toggle ---------------------------------------------------


def test_timestamp_toggle_changes_rendered_lines():
    wall = time.time() - 4321
    stamp = time.strftime("%H:%M:%S", time.localtime(wall))
    lines = [log_line(0, "blob", "hello world", wall=wall)]
    plain = export(
        azctl.render_logs(
            lines, combined=False, timestamps=False, service="blob", ever_started=True, height=6
        )
    )
    stamped = export(
        azctl.render_logs(
            lines, combined=False, timestamps=True, service="blob", ever_started=True, height=6
        )
    )
    assert stamp not in plain
    assert "%s hello world" % stamp in stamped


def test_timestamps_are_stored_at_arrival_so_toggle_is_retroactive():
    store = azctl.LogStore()
    store.append("blob", "early line")
    line = store.lines("blob")[0]
    expected = time.strftime("%H:%M:%S", time.localtime(line.wall))
    # Toggling on later still shows the arrival time recorded on the line.
    out = export(
        azctl.render_logs(
            store.lines("blob"),
            combined=False,
            timestamps=True,
            service="blob",
            ever_started=True,
            height=6,
        )
    )
    assert expected in out


def test_t_key_toggles_timestamps_with_note():
    dash = make_dashboard()
    assert dash.ui.timestamps is False
    dash.handle_event(key("t"))
    assert dash.ui.timestamps is True
    assert dash.ui.message[0] == "Timestamps on."
    dash.handle_event(key("t"))
    assert dash.ui.timestamps is False


# --- terminal bell on ->broken ------------------------------------------


class _FakeEventQueue:
    """Empty on the first drain (lets one refresh/render happen), then 'q'."""

    def __init__(self):
        self._drained_once = False

    def get_nowait(self):
        if not self._drained_once:
            self._drained_once = True
            raise queue.Empty
        return azctl.KeyEvent("q")


class _FakeReader:
    def __init__(self):
        self.events = _FakeEventQueue()

    def start(self):
        pass

    def stop(self):
        pass


class _TransitionManager(FakeManager):
    def __init__(self, transitions):
        FakeManager.__init__(self)
        self._transitions = list(transitions)

    def refresh(self):
        if self._transitions:
            return [self._transitions.pop(0)]
        return []


def _run_bounded_dashboard(monkeypatch, manager):
    sink = io.StringIO()
    console = Console(file=sink, force_terminal=True, width=80, height=24)
    monkeypatch.setattr(azctl, "Console", lambda: console)
    monkeypatch.setattr(azctl, "make_input_reader", _FakeReader)
    monkeypatch.setattr(azctl, "detect_versions", lambda: ("unknown", "unknown"))
    dash = azctl.Dashboard(azctl.Config(), manager)
    assert dash.run() == 0
    return sink.getvalue()


def test_bell_rings_on_transition_into_broken(monkeypatch):
    manager = _TransitionManager([azctl.Transition("blob", azctl.STARTING, azctl.BROKEN)])
    output = _run_bounded_dashboard(monkeypatch, manager)
    assert "\a" in output


def test_no_bell_without_broken_transition(monkeypatch):
    manager = _TransitionManager([azctl.Transition("blob", azctl.STARTING, azctl.RUNNING)])
    output = _run_bounded_dashboard(monkeypatch, manager)
    assert "\a" not in output


class _ReasonManager(_TransitionManager):
    """Like _TransitionManager, but reports a distinguishing BROKEN reason —
    the Node-missing message must reach the footer via the same refresh
    cycle that rings the bell, not just via a synchronous Start press."""

    def broken_reason(self, name):
        return "Node.js runtime not found — install Node, then npm install -g azurite"


def test_broken_transition_with_reason_shows_the_red_message(monkeypatch):
    manager = _ReasonManager([azctl.Transition("blob", azctl.STARTING, azctl.BROKEN)])
    output = _run_bounded_dashboard(monkeypatch, manager)
    assert "\a" in output
    assert "Node.js runtime not found" in output


def test_run_loop_shows_too_small_message_below_minimum_height(monkeypatch):
    manager = FakeManager()
    sink = io.StringIO()
    console = Console(file=sink, force_terminal=True, width=80, height=azctl.MIN_TERMINAL_HEIGHT - 1)
    monkeypatch.setattr(azctl, "Console", lambda: console)
    monkeypatch.setattr(azctl, "make_input_reader", _FakeReader)
    monkeypatch.setattr(azctl, "detect_versions", lambda: ("unknown", "unknown"))
    dash = azctl.Dashboard(azctl.Config(), manager)
    assert dash.run() == 0
    out = sink.getvalue()
    assert "too small" in out
    assert "[Start]" not in out


# --- signal handling: SIGHUP / re-entrant SIGTERM during shutdown -------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery only")
def test_sighup_triggers_clean_shutdown(monkeypatch):
    """BEHAVIOR.md: closing the terminal (SIGHUP) must shut services down
    cleanly, not orphan them. Reproduced by delivering a real SIGHUP to this
    process while Dashboard.run() is alive; the finally block's
    manager.shutdown() must run instead of the interpreter dying silently."""

    class _NeverReader:
        def __init__(self):
            self.events = queue.Queue()

        def start(self):
            pass

        def stop(self):
            pass

    manager = FakeManager(owned={"blob"})
    sink = io.StringIO()
    console = Console(file=sink, force_terminal=True, width=80, height=24)
    monkeypatch.setattr(azctl, "Console", lambda: console)
    monkeypatch.setattr(azctl, "make_input_reader", _NeverReader)
    monkeypatch.setattr(azctl, "detect_versions", lambda: ("unknown", "unknown"))
    dash = azctl.Dashboard(azctl.Config(), manager)

    def send_hup():
        time.sleep(0.2)
        os.kill(os.getpid(), signal.SIGHUP)

    threading.Thread(target=send_hup, daemon=True).start()
    assert dash.run() == 0
    assert ("shutdown",) in manager.calls
    assert dash.exit_note == "Interrupted — stopped all services."


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery only")
def test_second_sigterm_during_shutdown_does_not_abort_it(monkeypatch):
    """A second SIGINT/SIGTERM arriving while shutdown() is mid-flight (e.g.
    a impatient double Ctrl-C, or systemd's TERM-then-KILL pattern) must not
    raise KeyboardInterrupt a second time inside the finally block and abort
    the cleanup loop before every service has been stopped."""

    class _SlowShutdownManager(FakeManager):
        def __init__(self, owned=(), delay=0.4):
            FakeManager.__init__(self, owned)
            self.delay = delay
            self.shutdown_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1
            time.sleep(self.delay)
            self.calls.append(("shutdown",))
            self.owned.clear()

    class _NeverReader:
        def __init__(self):
            self.events = queue.Queue()

        def start(self):
            pass

        def stop(self):
            pass

    manager = _SlowShutdownManager(owned={"blob", "queue"}, delay=0.4)
    sink = io.StringIO()
    console = Console(file=sink, force_terminal=True, width=80, height=24)
    monkeypatch.setattr(azctl, "Console", lambda: console)
    monkeypatch.setattr(azctl, "make_input_reader", _NeverReader)
    monkeypatch.setattr(azctl, "detect_versions", lambda: ("unknown", "unknown"))
    dash = azctl.Dashboard(azctl.Config(), manager)

    def send_signals():
        time.sleep(0.15)
        os.kill(os.getpid(), signal.SIGTERM)  # -> KeyboardInterrupt -> finally/shutdown()
        time.sleep(0.15)  # lands mid-shutdown (delay=0.4s)
        os.kill(os.getpid(), signal.SIGTERM)  # must be ignored, not abort cleanup

    threading.Thread(target=send_signals, daemon=True).start()
    assert dash.run() == 0
    assert manager.shutdown_calls == 1  # ran exactly once, to completion
    assert ("shutdown",) in manager.calls  # the loop over SERVICE_ORDER finished


# --- async dispatch of confirmed actions (no UI freeze) ------------------


def test_slow_confirm_action_does_not_block_handle_event():
    """A real Stop/Restart/Free-port confirm can block for seconds inside
    kill_pid()'s wait; handle_event() must return almost immediately and let
    the result arrive later instead of freezing the render/input loop."""

    class _SlowManager(FakeManager):
        def stop(self, name):
            time.sleep(1.0)
            self.calls.append(("stop", name))
            self.owned.discard(name)
            return True, "Stopped %s." % name.capitalize(), "green"

    dash = make_dashboard(_SlowManager(owned={"blob"}))
    dash._activate(1)  # Stop -> asks
    start = time.monotonic()
    dash.handle_event(key("enter"))  # confirms
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, "handle_event blocked on a slow action (%.3fs)" % elapsed
    assert dash.ui.message[1] == "grey"  # immediate interim message
    assert ("stop", "blob") not in dash.manager.calls  # still running in the background
    assert wait_for(lambda: ("stop", "blob") in dash.manager.calls, timeout=3)
    assert wait_for(lambda: dash._pending_done is not None and dash._pending_done.is_set(), timeout=3)
    dash._poll_pending()  # what the run() loop does every tick
    assert dash.ui.message[0] == "Stopped Blob."


def test_busy_guard_prevents_duplicate_dispatch_while_action_in_flight():
    class _SlowManager(FakeManager):
        def stop(self, name):
            time.sleep(0.3)
            self.calls.append(("stop", name))
            self.owned.discard(name)
            return True, "Stopped %s." % name.capitalize(), "green"

    dash = make_dashboard(_SlowManager(owned={"blob"}))
    dash._activate(1)
    dash.handle_event(key("enter"))
    assert dash._busy is True
    dash._activate(1)  # try to Stop again while the first Stop is still running
    assert dash.ui.mode == "normal"  # did not re-ask for confirmation
    assert "finishing" in dash.ui.message[0]
    # Simulate the run() loop's per-tick drain until the background stop()
    # finishes and clears the busy flag.
    assert wait_for(lambda: (dash._poll_pending(), not dash._busy)[1], timeout=2)


# --- three-way quit state machine ---------------------------------------


def test_q_with_nothing_owned_quits_quietly():
    dash = make_dashboard(FakeManager())
    dash.handle_event(key("q"))
    assert dash.running is False
    assert dash.ui.mode == "normal"  # no question was asked
    assert dash.exit_note is None


def test_q_with_owned_services_asks_three_way_question():
    dash = make_dashboard(FakeManager(owned={"blob"}))
    dash.handle_event(key("q"))
    assert dash.running is True
    assert dash.ui.mode == "quit"
    out = export(azctl.render_footer(dash.ui, now=time.time()), width=140)
    assert "Enter: stop them and quit" in out
    assert "n: leave them running" in out
    assert "Esc: stay" in out


def test_quit_enter_stops_services_and_exits():
    manager = FakeManager(owned={"blob", "queue"})
    dash = make_dashboard(manager)
    dash.handle_event(key("q"))
    dash.handle_event(key("enter"))
    assert ("shutdown",) in manager.calls
    assert dash.running is False
    assert dash.detached is False
    assert dash.exit_note == "Stopped all services."


def test_quit_n_leaves_services_running_and_exits():
    manager = FakeManager(owned={"blob"})
    dash = make_dashboard(manager)
    dash.handle_event(key("q"))
    dash.handle_event(key("n"))
    assert ("detach_all",) in manager.calls
    assert ("shutdown",) not in manager.calls
    assert dash.running is False
    assert dash.detached is True
    assert "running" in dash.exit_note


def test_quit_esc_stays_in_dashboard():
    manager = FakeManager(owned={"blob"})
    dash = make_dashboard(manager)
    dash.handle_event(key("q"))
    dash.handle_event(key("esc"))
    assert dash.running is True
    assert dash.ui.mode == "normal"
    assert ("shutdown",) not in manager.calls


def test_quit_prompt_ignores_every_other_key():
    manager = FakeManager(owned={"blob"})
    dash = make_dashboard(manager)
    dash.handle_event(key("q"))
    for other in ("x", "q", "a", "up", "left", " "):
        dash.handle_event(key(other))
        assert dash.running is True, other
        assert dash.ui.mode == "quit", other
    assert ("shutdown",) not in manager.calls
    assert ("detach_all",) not in manager.calls
