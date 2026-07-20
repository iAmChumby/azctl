"""Shared test helpers: ephemeral ports, fake service scripts, polling."""

import socket
import subprocess
import sys
import time

# Fake "service" bodies for python -c, driven through ServiceManager's
# injectable command_for seam. No Azurite required anywhere.
LISTENER_SCRIPT = (
    "import socket, sys, time\n"
    "s = socket.socket()\n"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "s.bind(('127.0.0.1', int(sys.argv[1])))\n"
    "s.listen(5)\n"
    "time.sleep(120)\n"
)
SLEEPER_SCRIPT = "import time; time.sleep(120)"
DIER_SCRIPT = "import sys; sys.exit(3)"


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def make_config(azctl, tmp_path):
    return azctl.Config(
        blob_port=free_port(),
        queue_port=free_port(),
        table_port=free_port(),
        data_dir=str(tmp_path),
    )


def listener_command(name, config):
    return [sys.executable, "-c", LISTENER_SCRIPT, str(config.port_for(name))]


def spawn_listener(port):
    """A real separate process holding a port (for kill tests)."""
    proc = subprocess.Popen([sys.executable, "-c", LISTENER_SCRIPT, str(port)])
    assert wait_for(lambda: _open(port)), "listener never came up"
    return proc


def _open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
