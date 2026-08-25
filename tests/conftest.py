import importlib.util
import sys
from pathlib import Path

# Make the single-file app importable from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The app's TUI classes only exist when textual/rich are importable, and the
# engine/port tests exercise psutil-backed kill/lookup paths whose behaviour
# differs without it (kill_pid falls back to raw os.kill). A contributor who
# clones the repo and runs bare `pytest` should see clean skips, not 87
# AttributeErrors — CI always installs deps first, so this only affects
# local, pre-bootstrap runs.
_HAVE_DEPS = (
    importlib.util.find_spec("textual") is not None
    and importlib.util.find_spec("psutil") is not None
)

if not _HAVE_DEPS:
    collect_ignore = [
        "test_behavior.py",
        "test_engine.py",
        "test_integration.py",
        "test_ports.py",
        "test_render.py",
        "test_tui.py",
    ]
