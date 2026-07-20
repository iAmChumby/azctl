"""Dashboard event-handling logic — fake manager, no TTY, no Live loop."""

import azctl


class FakeManager:
    def __init__(self, owned=()):
        self.calls = []
        self.owned = set(owned)
        self.pids = {name: 1000 + i for i, name in enumerate(azctl.SERVICE_ORDER)}

    def start(self, name):
        self.calls.append(("start", name))
        return True, "Started %s." % name.capitalize(), "green"

    def stop(self, name):
        self.calls.append(("stop", name))
        if name in self.owned:
            self.owned.discard(name)
            return True, "Stopped %s." % name.capitalize(), "green"
        return True, "%s is not running." % name.capitalize(), "grey"

    def restart(self, name):
        self.calls.append(("restart", name))
        return True, "Started %s." % name.capitalize(), "green"

    def start_all(self):
        self.calls.append(("start_all",))
        return [self.start(n) for n in azctl.SERVICE_ORDER]

    def stop_all(self):
        self.calls.append(("stop_all",))
        return [self.stop(n) for n in list(azctl.SERVICE_ORDER)]

    def owns_live(self, name):
        return name in self.owned

    def any_owned(self):
        return bool(self.owned)

    def detach_all(self):
        self.calls.append(("detach_all",))

    def shutdown(self):
        self.calls.append(("shutdown",))

    def save_service_log(self, name):
        self.calls.append(("save", name))
        return True, "Wrote 3 lines to /tmp/azurite-%s.log" % name, "green"

    def save_merged_log(self):
        self.calls.append(("save_merged",))
        return True, "Wrote 9 lines to /tmp/azurite-all.log", "green"

    def notice_external_kill(self, name):
        self.calls.append(("notice_external_kill", name))

    def views(self):
        out = []
        for name in azctl.SERVICE_ORDER:
            pid = self.pids[name] if name in self.owned else None
            state = azctl.RUNNING if pid else azctl.STOPPED
            out.append(azctl.ServiceView(name, state, 10000, pid, None, bool(pid), None))
        return out


def dash(owned=()):
    fake_now = [100.0]
    d = azctl.Dashboard(
        azctl.Config(),
        FakeManager(owned),
        clock=lambda: fake_now[0],
        sleep=lambda _s: None,
    )
    d._now = fake_now
    return d


def key(d, k):
    d.handle_event(azctl.KeyEvent(k))


def test_up_down_select_with_grey_note():
    d = dash()
    key(d, "down")
    assert d.ui.selected == 1
    assert d.ui.message[0] == "Selected Queue." and d.ui.message[1] == "grey"
    key(d, "up")
    key(d, "up")
    assert d.ui.selected == 2  # wraps


def test_left_right_clamp():
    d = dash()
    key(d, "left")
    assert d.ui.button == 0
    for _ in range(20):
        key(d, "right")
    assert d.ui.button == len(azctl.BUTTONS) - 1


def test_enter_on_start_runs_immediately():
    d = dash()
    key(d, "enter")
    assert ("start", "blob") in d.manager.calls
    assert d.ui.message[1] == "green"


def test_stop_owned_asks_then_enter_confirms():
    d = dash(owned=("blob",))
    d.ui.button = 1  # Stop
    key(d, "enter")
    assert d.ui.mode == "confirm"
    assert d.ui.pending_prompt == "Stop Blob?"
    assert ("stop", "blob") not in d.manager.calls  # nothing happened yet
    key(d, "enter")
    assert d.ui.mode == "normal"
    assert ("stop", "blob") in d.manager.calls
    assert d.ui.message[0] == "Stopped Blob."


def test_confirm_cancelled_by_any_other_key():
    d = dash(owned=("blob",))
    d.ui.button = 1
    key(d, "enter")
    key(d, "x")  # not Esc, still cancels
    assert d.ui.mode == "normal"
    assert ("stop", "blob") not in d.manager.calls
    assert d.ui.message[0] == "Cancelled."


def test_stop_not_owned_needs_no_confirmation():
    d = dash()
    d.ui.button = 1
    key(d, "enter")
    assert d.ui.mode == "normal"
    assert d.ui.message == ("Blob is not running.", "grey", 104.0)


def test_restart_owned_asks_first():
    d = dash(owned=("queue",))
    d.ui.selected = 1
    d.ui.button = 2
    key(d, "enter")
    assert d.ui.mode == "confirm" and d.ui.pending_prompt == "Restart Queue?"
    key(d, "enter")
    assert ("restart", "queue") in d.manager.calls


def test_restart_not_owned_is_a_grey_note():
    d = dash()
    d.ui.button = 2
    key(d, "enter")
    assert d.ui.mode == "normal"
    assert d.ui.message[1] == "grey" and "not running" in d.ui.message[0]


def test_save_button_and_save_all_key():
    d = dash()
    d.ui.button = 3
    key(d, "enter")
    assert ("save", "blob") in d.manager.calls
    key(d, "S")
    assert ("save_merged",) in d.manager.calls


def test_stop_all_asks_and_aggregates():
    d = dash(owned=("blob", "queue"))
    d.ui.button = 6
    key(d, "enter")
    assert d.ui.mode == "confirm" and d.ui.pending_prompt == "Stop all services?"
    key(d, "enter")
    assert ("stop_all",) in d.manager.calls
    assert "Stopped Blob." in d.ui.message[0]


def test_stop_all_with_nothing_running_is_a_grey_note():
    d = dash()
    d.ui.button = 6
    key(d, "enter")
    assert d.ui.mode == "normal" and d.ui.message[1] == "grey"


def test_free_port_nothing_listening(monkeypatch):
    monkeypatch.setattr(azctl, "pid_on_port", lambda _p: None)
    monkeypatch.setattr(azctl, "port_open", lambda *_a, **_k: False)
    d = dash()
    d.ui.button = 4
    key(d, "enter")
    assert d.ui.mode == "normal"
    assert d.ui.message == ("Nothing is listening on port 10000.", "grey", 104.0)


def test_free_port_names_the_squatter(monkeypatch):
    monkeypatch.setattr(azctl, "pid_on_port", lambda _p: (24188, "node"))
    d = dash()
    d.ui.button = 4
    key(d, "enter")
    assert d.ui.mode == "confirm"
    assert d.ui.pending_prompt == "Kill node (PID 24188) on port 10000?"


def test_free_port_killing_our_own_child_reflects_stopped(monkeypatch):
    d = dash(owned=("blob",))
    my_pid = d.manager.pids["blob"]
    monkeypatch.setattr(azctl, "pid_on_port", lambda _p: (my_pid, "node"))
    killed = []
    monkeypatch.setattr(azctl, "kill_pid", lambda pid, **_k: killed.append(pid) or True)
    monkeypatch.setattr(azctl, "port_open", lambda *_a, **_k: bool(not killed))
    d.ui.button = 4
    key(d, "enter")
    key(d, "enter")  # confirm the kill
    assert killed == [my_pid]
    assert ("notice_external_kill", "blob") in d.manager.calls
    assert d.ui.message[1] == "green" and "port 10000 is free" in d.ui.message[0]


def test_quit_quiet_when_nothing_running():
    d = dash()
    key(d, "q")
    assert d.running is False
    assert d.exit_note is None
    assert ("shutdown",) not in d.manager.calls


def test_quit_three_way_enter_stops_all():
    d = dash(owned=("blob",))
    key(d, "q")
    assert d.ui.mode == "quit" and d.running is True
    key(d, "enter")
    assert d.running is False
    assert ("shutdown",) in d.manager.calls
    assert d.exit_note == "Stopped all services."


def test_quit_three_way_n_leaves_running():
    d = dash(owned=("blob",))
    key(d, "q")
    key(d, "n")
    assert d.running is False and d.detached is True
    assert ("detach_all",) in d.manager.calls
    assert ("shutdown",) not in d.manager.calls
    assert "running" in d.exit_note


def test_quit_three_way_esc_stays_and_ignores_stray_keys():
    d = dash(owned=("blob",))
    key(d, "q")
    key(d, "x")  # stray key: ignored, still asking
    assert d.ui.mode == "quit" and d.running is True
    key(d, "esc")
    assert d.ui.mode == "normal" and d.running is True


def test_toggles_and_overlays():
    d = dash()
    key(d, "a")
    assert d.ui.combined_logs is True and "on" in d.ui.message[0]
    key(d, "a")
    assert d.ui.combined_logs is False
    key(d, "t")
    assert d.ui.timestamps is True
    key(d, "?")
    assert d.ui.mode == "help"
    key(d, "x")  # any key closes
    assert d.ui.mode == "normal"


def test_connection_string_overlay_and_copy(monkeypatch):
    copied = []
    monkeypatch.setattr(azctl, "copy_osc52", lambda text, stream=None: copied.append(text))
    d = dash()
    d.ui.selected = 2
    key(d, "c")
    assert d.ui.mode == "conn"
    assert copied and "TableEndpoint=" in copied[0]
    assert d.ui.message[1] == "green" and "OSC 52" in d.ui.message[0]
    key(d, "q")  # any key closes the overlay instead of acting
    assert d.ui.mode == "normal" and d.running is True


def test_mouse_click_selects_row():
    d = dash()
    d.size = (100, 30)
    d.handle_event(azctl.MouseEvent(x=5, y=azctl.TABLE_ROW_Y0 + 2, pressed=True))
    assert d.ui.selected == 2
    assert d.ui.message[0] == "Selected Table."


def test_mouse_click_activates_button_but_confirms_still_gate():
    d = dash(owned=("blob",))
    d.size = (100, 30)
    _, x0, x1 = azctl.button_spans()[1]  # Stop
    y = 30 - azctl.BUTTON_ROW_FROM_BOTTOM
    d.handle_event(azctl.MouseEvent(x=(x0 + x1) // 2, y=y, pressed=True))
    assert d.ui.button == 1
    assert d.ui.mode == "confirm"  # a click can never skip the modal
    assert ("stop", "blob") not in d.manager.calls


def test_mouse_release_and_out_of_bounds_ignored():
    d = dash()
    d.size = (100, 30)
    d.handle_event(azctl.MouseEvent(x=5, y=azctl.TABLE_ROW_Y0, pressed=False))
    d.handle_event(azctl.MouseEvent(x=5, y=1, pressed=True))
    assert d.ui.selected == 0 and d.ui.message is None


def test_mouse_click_ignored_below_minimum_terminal_height():
    """Below MIN_TERMINAL_HEIGHT the button row isn't where the static
    hit-testing arithmetic assumes (Rich clips footer rows short), so a
    click there must not fire a button at all."""
    d = dash(owned=("blob",))
    d.size = (100, azctl.MIN_TERMINAL_HEIGHT - 1)
    _, x0, x1 = azctl.button_spans()[1]  # Stop
    y = d.size[1] - azctl.BUTTON_ROW_FROM_BOTTOM
    d.handle_event(azctl.MouseEvent(x=(x0 + x1) // 2, y=y, pressed=True))
    assert d.ui.mode == "normal"
    assert ("stop", "blob") not in d.manager.calls


def test_enter_on_confirm_is_ignored_below_minimum_terminal_height():
    """The confirmation question itself wouldn't have been drawn on a
    too-short terminal — Enter must not blindly act on a prompt the user
    could not have read (a stop/kill happening with zero visible warning)."""
    d = dash(owned=("blob",))
    d.ui.button = 1  # Stop
    key(d, "enter")  # opens the confirm prompt
    assert d.ui.mode == "confirm"
    d.size = (80, azctl.MIN_TERMINAL_HEIGHT - 1)
    key(d, "enter")  # would normally confirm; must be treated as cancel
    assert d.ui.mode == "normal"
    assert d.ui.message[0] == "Cancelled."
    assert ("stop", "blob") not in d.manager.calls


def test_quit_prompt_ignored_below_minimum_terminal_height():
    d = dash(owned=("blob",))
    key(d, "q")
    assert d.ui.mode == "quit"
    d.size = (80, azctl.MIN_TERMINAL_HEIGHT - 1)
    key(d, "enter")  # would normally stop everything and quit
    assert d.ui.mode == "quit" and d.running is True
    assert ("shutdown",) not in d.manager.calls
    key(d, "esc")  # the one response still honoured: stay
    assert d.ui.mode == "normal" and d.running is True


def test_ctrl_c_exits():
    d = dash(owned=("blob",))
    key(d, "ctrl-c")
    assert d.running is False
    assert "Interrupted" in d.exit_note


def test_message_fades_after_ttl():
    d = dash()
    key(d, "down")
    text, style, expires = d.ui.message
    assert expires == 100.0 + azctl.MESSAGE_TTL
