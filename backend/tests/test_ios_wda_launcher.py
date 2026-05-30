from __future__ import annotations

import os
from pathlib import Path

from ai_phone.agent.drivers import ios_wda_launcher as wda_launcher_module
from ai_phone.agent.drivers.ios_wda_launcher import IosWdaXcodeLauncher


def _launcher(
    *,
    bundle_id: str | None = "com.example.wda",
    team_id: str | None = "ABCDE12345",
) -> IosWdaXcodeLauncher:
    return IosWdaXcodeLauncher(
        udid="00008150-00041CAE3478401C",
        project_dir=Path("/tmp"),
        bundle_id=bundle_id,
        team_id=team_id,
    )


def test_build_cmd_includes_runner_bundle_id(monkeypatch):
    monkeypatch.setattr(wda_launcher_module, "_find_xcodebuild", lambda: "/usr/bin/xcodebuild")

    cmd = _launcher()._build_cmd()

    assert "PRODUCT_BUNDLE_IDENTIFIER=com.example.wda" in cmd
    assert "WDA_PRODUCT_BUNDLE_IDENTIFIER=com.example.wda.xctrunner" in cmd
    assert "DEVELOPMENT_TEAM=ABCDE12345" in cmd


def test_xcodebuild_env_overrides_stale_runner_bundle_id(monkeypatch):
    monkeypatch.setenv("WDA_PRODUCT_BUNDLE_IDENTIFIER", "com.old.wda.xctrunner")

    env = _launcher(bundle_id="com.new.wda")._xcodebuild_env()

    assert env is not None
    assert env["WDA_PRODUCT_BUNDLE_IDENTIFIER"] == "com.new.wda.xctrunner"
    assert os.environ["WDA_PRODUCT_BUNDLE_IDENTIFIER"] == "com.old.wda.xctrunner"


def test_empty_bundle_id_leaves_project_defaults():
    launcher = _launcher(bundle_id=None)

    assert launcher._runner_bundle_id() is None
    assert launcher._xcodebuild_env() is None
    assert all(not item.startswith("PRODUCT_BUNDLE_IDENTIFIER=") for item in launcher._build_cmd())
    assert all(not item.startswith("WDA_PRODUCT_BUNDLE_IDENTIFIER=") for item in launcher._build_cmd())
