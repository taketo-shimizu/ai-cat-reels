from __future__ import annotations

from pathlib import Path

import pytest

from app import config as cfg_module
from app import db as db_module
from app import main as main_module


def test_dry_run_flag_parses_from_subcommand() -> None:
    """回帰テスト: `publish --dry-run` でも dry-run が有効になること。"""
    parser = main_module.build_parser()
    args = parser.parse_args(["publish", "--dry-run"])
    assert args.dry_run is True
    args_no_dry = parser.parse_args(["publish"])
    assert args_no_dry.dry_run is False


def test_dry_run_env_overrides_namespace(cfg) -> None:
    """DRY_RUN=true の .env と args.dry_run=False が組み合わさっても dry-run は維持される。"""
    cfg_env_dry = cfg.__class__(**{**cfg.__dict__, "dry_run": True})
    result = main_module._apply_dry_run(cfg_env_dry, False)
    assert result.dry_run is True


def test_cli_init_creates_db(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(cfg_module, "DB_PATH", db_path)
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    monkeypatch.setattr(cfg_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cfg_module, "VIDEOS_DIR", tmp_path / "data/videos")
    monkeypatch.setattr(cfg_module, "TMP_DIR", tmp_path / "data/tmp")
    monkeypatch.setattr(cfg_module, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(cfg_module, "ENV_PATH", tmp_path / "noenv")
    for k in ("IG_ACCESS_TOKEN","IG_USER_ID","KLING_ACCESS_KEY","KLING_SECRET_KEY","ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    rc = main_module.main(["init"])
    # 必須キー未設定なので warning で rc=1
    assert rc == 1
    assert db_path.exists()


def test_daily_skips_if_already_posted(monkeypatch, cfg, tmp_db) -> None:
    with db_module.open_db() as conn:
        conn.execute(
            "INSERT INTO generations(kling_prompt, status, created_at)"
            " VALUES ('p', 'posted', '2026-01-01T00:00:00+00:00')"
        )
        gen_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        from datetime import date
        today = date.today().isoformat()
        conn.execute(
            "INSERT INTO posts(ig_media_id, generation_id, permalink, posted_at, like_count, comments_count, metrics_updated_at)"
            f" VALUES ('X', ?, 'p', '{today}T12:00:00+00:00', 0, 0, '{today}T12:00:00+00:00')",
            (gen_id,),
        )

    calls: list[str] = []
    monkeypatch.setattr(main_module, "cmd_metrics", lambda a, c: calls.append("metrics") or 0)
    monkeypatch.setattr(main_module, "cmd_plan", lambda a, c: calls.append("plan") or 0)

    import argparse
    ns = argparse.Namespace(dry_run=False)
    rc = main_module.cmd_daily(ns, cfg)
    assert rc == 0
    assert calls == []  # 二重投稿ガードで何も呼ばれない
