"""Config file, CLI flag precedence, and bootstrap path logic (all pure)."""

import argparse
import json
import pathlib
from types import SimpleNamespace

import pytest

import azctl


def ns(**overrides):
    base = {"host": None, "blob_port": None, "queue_port": None, "table_port": None, "data_dir": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_config_path_respects_xdg():
    path = azctl.config_path({"XDG_CONFIG_HOME": "/xdg"}, "/home/u", False)
    assert path == pathlib.Path("/xdg") / "azctl" / "config.json"


def test_config_path_default_posix():
    path = azctl.config_path({}, "/home/u", False)
    assert path == pathlib.Path("/home/u") / ".config" / "azctl" / "config.json"


def test_config_path_windows_appdata():
    path = azctl.config_path({"APPDATA": "C:/Users/u/AppData/Roaming"}, "C:/Users/u", True)
    assert path == pathlib.Path("C:/Users/u/AppData/Roaming") / "azctl" / "config.json"


def test_bootstrap_venv_dir_respects_xdg_cache():
    path = azctl.bootstrap_venv_dir({"XDG_CACHE_HOME": "/cache"}, "/home/u", False)
    assert path == pathlib.Path("/cache") / "azctl" / "venv"


def test_bootstrap_venv_dir_default_posix():
    path = azctl.bootstrap_venv_dir({}, "/home/u", False)
    assert path == pathlib.Path("/home/u") / ".cache" / "azctl" / "venv"


def test_bootstrap_venv_dir_windows_localappdata():
    path = azctl.bootstrap_venv_dir({"LOCALAPPDATA": "C:/Users/u/AppData/Local"}, "C:/Users/u", True)
    assert path == pathlib.Path("C:/Users/u/AppData/Local") / "azctl" / "venv"
    vpy = azctl.bootstrap_venv_python(path, True)
    assert vpy.name == "python.exe" and vpy.parent.name == "Scripts"


def test_load_config_missing_file_is_empty(tmp_path):
    assert azctl.load_config(tmp_path / "nope.json") == {}


def test_load_config_invalid_json_warns_and_ignores(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    assert azctl.load_config(path) == {}
    assert "warning" in capsys.readouterr().err


def test_load_config_non_dict_warns_and_ignores(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text('["a", "list"]', encoding="utf-8")
    assert azctl.load_config(path) == {}
    assert "warning" in capsys.readouterr().err


def test_load_config_bad_port_skipped_good_keys_kept(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"host": "0.0.0.0", "blob_port": "not-a-port", "unknown": 1}), encoding="utf-8")
    loaded = azctl.load_config(path)
    assert loaded == {"host": "0.0.0.0"}
    assert "blob_port" in capsys.readouterr().err


def test_resolve_precedence_defaults_file_flags():
    cfg = azctl.resolve_config(ns(blob_port=1234), {"blob_port": 999, "host": "0.0.0.0"})
    assert cfg.blob_port == 1234  # CLI flag beats file
    assert cfg.host == "0.0.0.0"  # file beats default
    assert cfg.queue_port == 10001  # default survives
    assert "~" not in cfg.data_dir  # expanded


def test_bootstrap_sentinel_guard_explains_and_exits(monkeypatch, capsys):
    monkeypatch.setenv(azctl.BOOTSTRAP_SENTINEL, "1")
    with pytest.raises(SystemExit) as excinfo:
        azctl._bootstrap_and_reexec()
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "pip install textual psutil" in err
    assert "Traceback" not in err


# --- venv health / corrupted-install detection (not just vpy.exists()) ----


def test_venv_has_pip_false_when_python_missing(tmp_path):
    assert azctl._venv_has_pip(tmp_path / "no" / "python") is False


def test_venv_has_pip_true_when_probe_succeeds(tmp_path):
    fake_py = tmp_path / "python"
    fake_py.write_text("")

    def runner(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout="pip 24.0", stderr="")

    assert azctl._venv_has_pip(fake_py, runner=runner) is True


def test_venv_has_pip_false_when_pip_module_missing(tmp_path):
    """The real `python3 -m venv --without-pip` / Debian ensurepip-missing
    symptom: the interpreter runs fine, but `-m pip` has nothing to import."""
    fake_py = tmp_path / "python"
    fake_py.write_text("")

    def runner(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="No module named pip")

    assert azctl._venv_has_pip(fake_py, runner=runner) is False


def test_venv_has_pip_false_when_runner_raises(tmp_path):
    fake_py = tmp_path / "python"
    fake_py.write_text("")

    def runner(cmd, **kwargs):
        raise OSError("exec format error")

    assert azctl._venv_has_pip(fake_py, runner=runner) is False


def test_deps_importable_reflects_probe_returncode(tmp_path):
    fake_py = tmp_path / "python"
    ok_runner = lambda cmd, **k: SimpleNamespace(returncode=0)  # noqa: E731
    bad_runner = lambda cmd, **k: SimpleNamespace(returncode=1)  # noqa: E731
    assert azctl._deps_importable(fake_py, runner=ok_runner) is True
    assert azctl._deps_importable(fake_py, runner=bad_runner) is False


def test_deps_importable_false_when_runner_raises(tmp_path):
    def runner(cmd, **kwargs):
        raise OSError("boom")

    assert azctl._deps_importable(tmp_path / "python", runner=runner) is False


# --- bootstrap gating: read-only commands don't need deps (item 11) -------


def test_command_needs_deps_exempts_read_only_commands():
    for cmd in ("status", "watch", "free-ports"):
        assert azctl.command_needs_deps(cmd) is False


def test_command_needs_deps_true_for_the_dashboard():
    for cmd in (None, "up"):
        assert azctl.command_needs_deps(cmd) is True


def test_main_does_not_bootstrap_for_status_when_deps_missing(monkeypatch, tmp_path, capsys):
    """Item 11: status/watch/free-ports must work even when rich/psutil
    haven't been bootstrapped yet — they already have complete _HAVE_DEPS
    fallbacks, so gating them behind a network-touching bootstrap made that
    fallback code dead and left the user with zero status information."""
    monkeypatch.setattr(azctl, "_HAVE_DEPS", False)

    def bomb():
        raise AssertionError("bootstrap must not run for a read-only command")

    monkeypatch.setattr(azctl, "_bootstrap_and_reexec", bomb)
    monkeypatch.setattr(azctl, "config_path", lambda *a, **k: tmp_path / "nope.json")
    rc = azctl.main(["status", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["services"]) == {"blob", "queue", "table"}


def test_main_still_bootstraps_for_the_dashboard_when_deps_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(azctl, "_HAVE_DEPS", False)
    called = []
    monkeypatch.setattr(azctl, "_bootstrap_and_reexec", lambda: called.append(True))
    monkeypatch.setattr(azctl, "config_path", lambda *a, **k: tmp_path / "nope.json")
    monkeypatch.setattr(azctl, "run_dashboard", lambda *a, **k: 2)
    rc = azctl.main([])
    assert called == [True]
    assert rc == 2
