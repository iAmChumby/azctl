"""Optional integration tests against a real Azurite install.

Skipped automatically when azurite is not on PATH (as in the CI test jobs).
"""

import shutil

import pytest

import azctl
from helpers import make_config, wait_for

pytestmark = pytest.mark.skipif(
    shutil.which("azurite-blob") is None, reason="azurite is not installed"
)


def test_real_azurite_blob_lifecycle(tmp_path):
    cfg = make_config(azctl, tmp_path)
    mgr = azctl.ServiceManager(cfg)
    ok, msg, _style = mgr.start("blob")
    assert ok, msg
    try:
        assert wait_for(
            lambda: (mgr.refresh(), mgr.views()[0].state)[1] == azctl.RUNNING,
            timeout=30,
        ), "azurite-blob never reached running"
    finally:
        mgr.shutdown()
    mgr.refresh()
    assert mgr.views()[0].state == azctl.STOPPED
    assert not azctl.port_open(cfg.host, cfg.blob_port)
