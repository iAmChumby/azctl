"""Snapshot-style tests of the pure render functions (no TTY, recorded console)."""

import io

from rich.console import Console

import azctl


def export(renderable, width=100, height=None):
    # file=io.StringIO(): a record=True Console still physically writes to
    # its `file` in addition to recording -- an explicit in-memory sink means
    # this never touches the real stdout (whose encoding is whatever the
    # terminal/OS default is, e.g. legacy cp1252 on a Windows CI runner,
    # which can't represent the box-drawing glyphs these renderables use).
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


def test_sparkline_empty_when_no_data():
    assert azctl.render_sparkline(()) == ""
    assert azctl.render_sparkline([]) == ""


def test_sparkline_scales_to_peak():
    out = azctl.render_sparkline((1, 2, 3, 4))
    assert len(out) == 4
    blocks = [azctl.SPARK_BLOCKS.index(ch) for ch in out]
    assert blocks == sorted(blocks), "higher counts must map to taller blocks"
    assert out[-1] == azctl.SPARK_BLOCKS[-1], "the peak always uses the tallest block"


def test_sparkline_flat_activity_uses_middle_blocks():
    out = azctl.render_sparkline((5, 5, 5))
    assert out == azctl.SPARK_BLOCKS[-1] * 3 == "█" * 3


def test_btn_label_hot_is_reversed_and_bracketed_tight():
    stop = azctl.BUTTONS[1]
    hot = azctl.btn_label(stop, hot=True)
    cold = azctl.btn_label(stop, hot=False)
    assert hot.plain == "[Stop]"
    assert cold.plain == "[ Stop ]"
    styles_hot = [str(s.style) for s in hot.spans]
    styles_cold = [str(s.style) for s in cold.spans]
    assert any("reverse" in s for s in styles_hot)
    assert not any("reverse" in s for s in styles_cold)


def test_btn_label_destructive_buttons_are_red():
    for btn in azctl.BUTTONS:
        if not btn.danger:
            continue
        for label in (azctl.btn_label(btn, hot=True), azctl.btn_label(btn, hot=False)):
            span_styles = [str(s.style) for s in label.spans]
            assert any("red" in s for s in span_styles), btn.label


def test_btn_label_safe_buttons_are_not_red():
    for btn in azctl.BUTTONS:
        if btn.danger:
            continue
        for label in (azctl.btn_label(btn, hot=True), azctl.btn_label(btn, hot=False)):
            span_styles = [str(s.style) for s in label.spans]
            assert not any("red" in s for s in span_styles), btn.label


def test_footer_legend_and_mode_indicator():
    ui = azctl.UIState()
    out = export(azctl.render_footer(ui, now=0.0), width=140)
    for state in ("running", "starting", "stopped", "broken", "port in use"):
        assert state in out
    assert "logs: selected" in out


def test_footer_combined_indicator():
    ui = azctl.UIState(combined_logs=True)
    out = export(azctl.render_footer(ui, now=0.0), width=140)
    assert "logs: ALL" in out


def test_footer_filter_indicator():
    ui = azctl.UIState(filter_text="blob")
    off = export(azctl.render_footer(azctl.UIState(), now=0.0), width=140)
    out = export(azctl.render_footer(ui, now=0.0), width=140)
    assert "/blob/" in out
    assert "/blob/" not in off


def test_footer_message_shown_then_fades():
    ui = azctl.UIState(message=("Started Blob (PID 7).", "green", 10.0))
    out = export(azctl.render_footer(ui, now=5.0))
    assert "Started Blob (PID 7)." in out
    out = export(azctl.render_footer(ui, now=11.0))
    assert "Started Blob" not in out


def test_footer_busy_spinner_while_working():
    ui = azctl.UIState()
    out = export(azctl.render_footer(ui, now=0.0, busy_spinner="⠋"), width=140)
    assert "working…" in out
    ui = azctl.UIState(message=("Stopping services…", "grey", 10.0))
    out = export(azctl.render_footer(ui, now=5.0, busy_spinner="⠙"), width=140)
    assert "⠙" in out and "Stopping services…" in out


def test_footer_mode_indicator_survives_truncation_at_normal_widths():
    """BEHAVIOR.md: 'The footer shows whether this mode is on or off.' The
    indicator is placed before the static hint text so the no_wrap+ellipsis
    line can never truncate it away at common terminal widths."""
    for width in (80, 100):
        off = export(azctl.render_footer(azctl.UIState(combined_logs=False), now=0.0), width=width)
        on = export(azctl.render_footer(azctl.UIState(combined_logs=True), now=0.0), width=width)
        assert "logs: selected" in off, width
        assert "logs: ALL" in on, width
        assert off != on, width


def test_help_overlay_lists_every_key_and_button():
    out = export(azctl.render_help())
    for key in ("Enter", "a", "t", "/", "c", "S", "?", "q", "Esc", "mouse"):
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


def test_button_bar_shape_is_stable():
    labels = [b.label for b in azctl.BUTTONS]
    assert labels == ["Start", "Stop", "Restart", "Save", "Free port", "Start all", "Stop all"]
    groups = [b.group for b in azctl.BUTTONS]
    assert groups[:5] == [0] * 5 and groups[5:] == [1, 1], (
        "per-service actions must be grouped apart from the all-services ones"
    )
    danger = {b.label for b in azctl.BUTTONS if b.danger}
    assert danger == {"Stop", "Restart", "Free port", "Stop all"}
