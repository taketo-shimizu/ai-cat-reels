"""共通フィクスチャ。DB パスを tmp に切り替えて本物の app.db を汚さない。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import config as cfg_module
from app import db as db_module
from app.config import Config


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(cfg_module, "DB_PATH", db_path)
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.init_db(db_path)
    return db_path


@pytest.fixture
def cfg() -> Config:
    return Config(
        ig_access_token="TEST_TOKEN",
        ig_user_id="123456",
        ig_graph_version="v23.0",
        kling_api_key="",
        kling_access_key="AK",
        kling_secret_key="SK",
        kling_base_url="https://api-singapore.klingai.com",
        kling_model="kling-v2-6",
        kling_mode="std",
        kling_duration="5",
        kling_sound="on",
        anthropic_api_key="ant_test",
        claude_model="claude-sonnet-4-6",
        dry_run=False,
    )
