"""The pure key/mouse byte parser — no terminal anywhere."""

import azctl


def keys(events):
    return [e.key for e in events if isinstance(e, azctl.KeyEvent)]


def test_arrows():
    events, rest = azctl.parse_bytes(b"\x1b[A\x1b[B\x1b[C\x1b[D")
    assert keys(events) == ["up", "down", "right", "left"]
    assert rest == b""


def test_ss3_arrows_application_mode():
    events, rest = azctl.parse_bytes(b"\x1bOA\x1bOB")
    assert keys(events) == ["up", "down"]
    assert rest == b""


def test_enter_and_printables_are_case_sensitive():
    events, rest = azctl.parse_bytes(b"\rSsq?")
    assert keys(events) == ["enter", "S", "s", "q", "?"]
    assert rest == b""


def test_ctrl_c():
    events, _ = azctl.parse_bytes(b"\x03")
    assert keys(events) == ["ctrl-c"]


def test_lone_escape_stays_in_remainder():
    events, rest = azctl.parse_bytes(b"\x1b")
    assert events == []
    assert rest == b"\x1b"


def test_escape_followed_by_plain_key_is_esc_then_key():
    events, rest = azctl.parse_bytes(b"\x1bq")
    assert keys(events) == ["esc", "q"]
    assert rest == b""


def test_incomplete_csi_stays_in_remainder():
    events, rest = azctl.parse_bytes(b"\x1b[<0;12")
    assert events == []
    assert rest == b"\x1b[<0;12"


def test_unknown_csi_consumed_and_dropped():
    events, rest = azctl.parse_bytes(b"\x1b[5~\x1b[200~q")
    assert keys(events) == ["q"]
    assert rest == b""


def test_sgr_mouse_press_and_release():
    events, rest = azctl.parse_bytes(b"\x1b[<0;12;5M\x1b[<0;12;5m")
    assert rest == b""
    assert len(events) == 2
    press, release = events
    assert isinstance(press, azctl.MouseEvent)
    assert (press.x, press.y, press.pressed) == (12, 5, True)
    assert (release.x, release.y, release.pressed) == (12, 5, False)


def test_sgr_mouse_non_left_buttons_dropped():
    # wheel up (64), right button (2), drag/motion (32)
    events, rest = azctl.parse_bytes(b"\x1b[<64;3;3M\x1b[<2;3;3M\x1b[<32;3;3M")
    assert events == []
    assert rest == b""


def test_mixed_stream():
    events, rest = azctl.parse_bytes(b"a\x1b[B\x1b[<0;1;7M\rX")
    assert rest == b""
    kinds = [e.key if isinstance(e, azctl.KeyEvent) else "mouse" for e in events]
    assert kinds == ["a", "down", "mouse", "enter", "X"]
