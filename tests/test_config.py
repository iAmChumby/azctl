"""Config file, CLI flag precedence, and bootstrap path logic (all pure)."""

import argparse
import json
import pathlib

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
    assert "pip install rich psutil" in err
    assert "Traceback" not in err
