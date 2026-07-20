"""Snapshot-style tests of the pure render functions (no TTY, recorded console)."""

import time

from rich.console import Console

import azctl


def export(renderable, width=100, height=None):
    console = Console(record=True, width=width, height=height)
    console.print(renderable)
    return console.export_text()


def view(name, state, port, pid=None, uptime=None, ever=False):
    return azctl.ServiceView(name, state, port, pid, uptime, ever, None)


ALL_STATE_VIEWS = [
    view("blob", azctl.RUNNING, 10000, pid=4242, uptime=3725, ever=True),
    view("queue", azctl.STARTING, 10001, pid=4243, uptime=2, ever=True),
    view("table", azctl.STOPPED, 10002),
]


def test_header_shows_host_data_dir_and_versions():
    cfg = azctl.Config(host="0.0.0.0", data_dir="/tmp/azdata")
    out = export(azctl.render_header(cfg, ("unknown", "unknown")), width=140)
    assert "azctl" in out
    assert "0.0.0.0" in out
    assert "/tmp/azdata" in out
    assert "azurite unknown" in out and "node unknown" in out


def test_table_marker_states_symbols_and_dashes():
    out = export(azctl.render_table(ALL_STATE_VIEWS, selected=0))
    assert "▸ Blob" in out
    assert "  Queue" in out
    assert "● running" in out
    assert "◐ starting" in out
    assert "○ stopped" in out
    assert "4242" in out and "1:02:05" in out
    # stopped row: dash for PID and uptime
    table_line = [ln for ln in out.splitlines() if "Table" in ln][0]
    assert table_line.count("—") == 2


def test_table_broken_and_port_in_use_symbols():
    views = [
        view("blob", azctl.BROKEN, 10000, ever=True),
        view("queue", azctl.PORT_IN_USE, 10001),
        view("table", azctl.STOPPED, 10002),
    ]
    out = export(azctl.render_table(views, selected=1))
    assert "✖ broken" in out
    assert "◆ port in use" in out
    assert "▸ Queue" in out


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


def test_footer_confirm_prompt_replaces_bar():
    ui = azctl.UIState(mode="confirm", pending_prompt="Stop Blob?")
    out = export(azctl.render_footer(ui, now=0.0))
    assert "Stop Blob?" in out
    assert "[Start]" not in out


def test_footer_quit_prompt():
    ui = azctl.UIState(mode="quit")
    out = export(azctl.render_footer(ui, now=0.0))
    assert "Enter: stop them and quit" in out
    assert "n: leave them running" in out
    assert "Esc: stay" in out


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


def test_build_frame_composes_all_regions():
    cfg = azctl.Config()
    ui = azctl.UIState(selected=2)  # Table: never started
    frame = azctl.build_frame(cfg, ALL_STATE_VIEWS, [], ui, height=30, now=0.0)
    out = export(frame, width=140, height=30)
    assert "azctl" in out
    assert "● running" in out
    assert "Table has never run" in out
    assert "[Start all]" in out


def test_button_spans_match_bar_layout():
    spans = azctl.button_spans()
    assert len(spans) == len(azctl.BUTTONS)
    # spans are ordered, non-overlapping, and 1-based
    assert spans[0][1] == 1
    for (i, x0, x1), (_, nx0, _) in zip(spans, spans[1:]):
        assert x1 < nx0
        assert x1 - x0 + 1 == len(azctl.BUTTONS[i].label) + 2
    # the group separator makes an extra-wide gap before "Start all"
    gap_normal = spans[1][1] - spans[0][2]
    gap_group = spans[5][1] - spans[4][2]
    assert gap_group > gap_normal
    # the rendered bar puts each [label] exactly at its span
    plain = azctl.render_button_bar(active=0).plain
    for i, x0, x1 in spans:
        assert plain[x0 - 1 : x1] == "[%s]" % azctl.BUTTONS[i].label
