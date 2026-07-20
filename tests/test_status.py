import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import azctl  # noqa: E402


def test_snapshot_reports_all_three_services():
    snap = azctl.observe(azctl.Config())
    services = snap["services"]
    assert set(services) == {"blob", "queue", "table"}
    assert services["blob"]["port"] == 10000
    assert services["queue"]["port"] == 10001
    assert services["table"]["port"] == 10002


def test_port_open_detects_listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert azctl.port_open("127.0.0.1", port)
    finally:
        srv.close()
    assert not azctl.port_open("127.0.0.1", port)


def test_status_json_is_machine_readable(capsys):
    rc = azctl.main(["status", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["services"]) == {"blob", "queue", "table"}
