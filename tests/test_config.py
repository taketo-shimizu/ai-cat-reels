from __future__ import annotations

from pathlib import Path

import pytest

from app import config as cfg_module


def test_load_config_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k in [
        "IG_ACCESS_TOKEN", "IG_USER_ID", "IG_GRAPH_VERSION",
        "KLING_ACCESS_KEY", "KLING_SECRET_KEY", "KLING_BASE_URL",
        "KLING_MODEL", "KLING_MODE", "KLING_DURATION",
        "ANTHROPIC_API_KEY", "CLAUDE_MODEL", "DRY_RUN",
    ]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cfg_module, "ENV_PATH", tmp_path / "noenv")
    cfg = cfg_module.load_config()
    assert cfg.ig_graph_version == "v23.0"
    assert cfg.kling_base_url == "https://api-singapore.klingai.com"
    assert cfg.kling_mode == "std"
    assert cfg.kling_duration == "5"
    assert cfg.claude_model.startswith("claude-")
    assert cfg.dry_run is False


def test_missing_env_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k in [
        "IG_ACCESS_TOKEN", "IG_USER_ID",
        "KLING_API_KEY", "KLING_ACCESS_KEY", "KLING_SECRET_KEY", "ANTHROPIC_API_KEY",
    ]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cfg_module, "ENV_PATH", tmp_path / "noenv")
    cfg = cfg_module.load_config()
    assert cfg_module.missing_env_keys(cfg, "collect") == ["IG_ACCESS_TOKEN", "IG_USER_ID"]
    assert cfg_module.missing_env_keys(cfg, "generate") == [
        "KLING_API_KEY or (KLING_ACCESS_KEY + KLING_SECRET_KEY)"
    ]
    assert cfg_module.missing_env_keys(cfg, "plan") == ["ANTHROPIC_API_KEY"]


def test_generate_accepts_either_credential(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cfg_module, "ENV_PATH", tmp_path / "noenv")
    for k in ("KLING_API_KEY", "KLING_ACCESS_KEY", "KLING_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)

    monkeypatch.setenv("KLING_API_KEY", "sk-new")
    assert cfg_module.missing_env_keys(cfg_module.load_config(), "generate") == []

    monkeypatch.delenv("KLING_API_KEY")
    monkeypatch.setenv("KLING_ACCESS_KEY", "AK")
    monkeypatch.setenv("KLING_SECRET_KEY", "SK")
    assert cfg_module.missing_env_keys(cfg_module.load_config(), "generate") == []


def test_read_lines_filters_comments(tmp_path: Path) -> None:
    f = tmp_path / "list.txt"
    f.write_text("# a comment\nalpha\n\nbeta\n#skip\n", encoding="utf-8")
    assert cfg_module.read_lines(f) == ["alpha", "beta"]
