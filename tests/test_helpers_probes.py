"""Connection strings, uptime formatting, version probes, dashboard guard."""

import azctl


def test_connection_string_shape():
    cfg = azctl.Config(host="127.0.0.1", blob_port=10000)
    cs = azctl.connection_string("blob", cfg)
    assert "DefaultEndpointsProtocol=http;" in cs
    assert "AccountName=devstoreaccount1;" in cs
    assert "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;" in cs


def test_connection_string_uses_configured_port():
    cfg = azctl.Config(host="0.0.0.0", table_port=12345)
    cs = azctl.connection_string("table", cfg)
    assert "TableEndpoint=http://0.0.0.0:12345/devstoreaccount1;" in cs


def test_format_uptime():
    assert azctl.format_uptime(0) == "0:00:00"
    assert azctl.format_uptime(3725) == "1:02:05"
    assert azctl.format_uptime(59.9) == "0:00:59"


def test_detect_versions_never_crashes(monkeypatch):
    monkeypatch.setattr(azctl.shutil, "which", lambda name: None)
    assert azctl.detect_versions() == ("unknown", "unknown")


def test_copy_osc52_writes_escape_sequence():
    class Sink:
        def __init__(self):
            self.data = ""

        def write(self, s):
            self.data += s

        def flush(self):
            pass

    sink = Sink()
    azctl.copy_osc52("hello", stream=sink)
    assert sink.data.startswith("\x1b]52;c;")
    assert sink.data.endswith("\x07")


def test_dashboard_refuses_without_a_tty(capsys):
    rc = azctl.run_dashboard(azctl.Config())
    assert rc == 2
    assert "interactive terminal" in capsys.readouterr().out
