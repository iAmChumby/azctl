"""Snapshot-style tests of the pure render functions (no TTY, recorded console)."""

import io
import time

from rich.console import Console

import azctl


def export(renderable, width=100, height=None):
    # file=io.StringIO(): a record=True Console still physically writes to
    # its `file` in addition to recording -- an explicit in-memory sink means
    # this never touches the real stdout (whose encoding is whatever the
    # terminal/OS default is, e.g. legacy cp1252 on a Windows CI runner,
    # which can't represent the box-drawing/bullet glyphs these renderables
    # use) even if nothing here happens to redirect stdout today.
    console = Console(record=True, width=width, height=height, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def test_header_shows_host_data_dir_and_versions():
    cfg = azctl.Config(host="0.0.0.0", data_dir="/tmp/azdata")
    out = export(azctl.render_header(cfg, ("unknown", "unknown")), width=140)
    assert "azctl" in out
    assert "0.0.0.0" in out
    assert "/tmp/azdata" in out
    assert "azurite unknown" in out and "node unknown" in out


# NOTE: the status table is now a Textual DataTable (AzctlApp._populate_table
# in azctl.py), not a rich Table built by a standalone render_table()
# function -- that function was deleted in the Textual port. Its
# symbol/colour/dash/uptime semantics are exercised through AzctlApp itself
# (tests/test_tui.py, rewritten separately) rather than here.


def test_logs_never_run_hint():
    out = export(
        azctl.render_logs(
            [], combined=False, timestamps=False, service="blob", ever_started=False, height=10
        )
    )
    assert "Blob has never run. Press Enter on [Start] to launch it." in out


def test_logs_combined_tags_and_timestamps():
    now = time.time()
    lines = [
        azctl.LogLine(0, 0.0, now, "blob", "hello from blob"),
        azctl.LogLine(1, 0.1, now, "queue", "hello from queue"),
        azctl.LogLine(2, 0.2, now, "table", "hello from table"),
    ]
    out = export(
        azctl.render_logs(
            lines, combined=True, timestamps=True, service="blob", ever_started=True, height=10
        )
    )
    assert "[blob] hello from blob" in out
    assert "[queue] hello from queue" in out
    assert "[table] hello from table" in out
    assert "all services (merged)" in out
    stamp = time.strftime("%H:%M:%S", time.localtime(now))
    assert stamp in out


def test_logs_long_lines_truncate_not_wrap():
    lines = [azctl.LogLine(0, 0.0, time.time(), "blob", "x" * 500)]
    out = export(
        azctl.render_logs(
            lines, combined=False, timestamps=False, service="blob", ever_started=True, height=10
        ),
        width=60,
    )
    body = [ln for ln in out.splitlines() if "x" in ln]
    assert len(body) == 1  # one noisy line never eats several rows
    assert "…" in body[0]


def test_logs_only_newest_lines_that_fit():
    lines = [azctl.LogLine(i, 0.0, time.time(), "blob", "line-%d" % i) for i in range(50)]
    out = export(
        azctl.render_logs(
            lines, combined=False, timestamps=False, service="blob", ever_started=True, height=7
        )
    )
    assert "line-49" in out
    assert "line-0" not in out


def test_footer_legend_buttons_and_separator():
    ui = azctl.UIState()
    out = export(azctl.render_footer(ui, now=0.0), width=140)
    for state in ("running", "starting", "stopped", "broken", "port in use"):
        assert state in out
    for btn in azctl.BUTTONS:
        assert "[%s]" % btn.label in out
    assert "│" in out  # visual separator before the all-services group
    assert "logs: selected" in out


# NOTE: confirm/quit prompts no longer live in UIState.mode / render_footer --
# they are ModalScreen overlays (ConfirmScreen/QuitScreen in azctl.py) that
# render their own prompt text and are exercised via AzctlApp (test_tui.py).


def test_footer_message_shown_then_fades():
    ui = azctl.UIState(message=("Started Blob (PID 7).", "green", 10.0))
    out = export(azctl.render_footer(ui, now=5.0))
    assert "Started Blob (PID 7)." in out
    out = export(azctl.render_footer(ui, now=11.0))
    assert "Started Blob" not in out


def test_footer_combined_indicator():
    ui = azctl.UIState(combined_logs=True)
    out = export(azctl.render_footer(ui, now=0.0), width=140)
    assert "logs: ALL" in out


def test_footer_mode_indicator_survives_truncation_at_normal_widths():
    """BEHAVIOR.md: 'The footer shows whether this mode is on or off, so you
    always know which view you are reading.' At the two most common default
    terminal widths (80, 100) the old ordering put the indicator after ~102
    chars of static hint text, so the no_wrap+ellipsis line truncated it away
    identically whether combined_logs was True or False."""
    for width in (80, 100):
        off = export(azctl.render_footer(azctl.UIState(combined_logs=False), now=0.0), width=width)
        on = export(azctl.render_footer(azctl.UIState(combined_logs=True), now=0.0), width=width)
        assert "logs: selected" in off, width
        assert "logs: ALL" in on, width
        assert off != on, width


def test_help_overlay_lists_every_key_and_button():
    out = export(azctl.render_help())
    for key in ("Enter", "a", "t", "c", "S", "?", "q", "Esc", "mouse"):
        assert key in out
    for btn in azctl.BUTTONS:
        assert btn.label in out


def test_conn_overlay_lists_all_three_strings():
    out = export(azctl.render_conn(azctl.Config(), selected=1), width=200)
    assert out.count("DefaultEndpointsProtocol=http;") == 3
    assert "BlobEndpoint=" in out
    assert "QueueEndpoint=" in out
    assert "TableEndpoint=" in out
    assert "▸ Queue" in out


# NOTE: build_frame() (the rich.Layout frame) and button_spans() (mouse
# hit-testing geometry) were both deleted in the Textual port -- Textual's
# CSS layout and DataTable/ModalScreen widgets replace them. Frame
# composition is exercised through AzctlApp (test_tui.py) instead.
