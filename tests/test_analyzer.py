from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import analyzer
from app import db as db_module


def _seed_ref(conn, media_id: str, media_url: str | None, caption: str = "cap", score: float = 1.0):
    conn.execute(
        """
        INSERT INTO reference_posts(
            media_id, source, caption, media_url, permalink,
            like_count, comments_count, view_count, posted_at, score, collected_at, analyzed
        ) VALUES (?, 'hashtag:x', ?, ?, 'p', 0, 0, NULL, '2026-01-01T00:00:00+00:00', ?, '2026-01-01T00:00:00+00:00', 0)
        """,
        (media_id, caption, media_url, score),
    )


def test_parse_json_strict_with_fence() -> None:
    text = "```json\n{\"a\": 1}\n```"
    assert analyzer._parse_json_strict(text) == {"a": 1}


def test_parse_json_strict_embedded() -> None:
    text = "ここに注釈 {\"b\": 2} その他"
    assert analyzer._parse_json_strict(text) == {"b": 2}


def test_pending_posts_prefers_url_over_score(tmp_db) -> None:
    """URL あり (低スコア) を URL なし (高スコア) より優先するか。"""
    from app import analyzer
    with db_module.open_db() as conn:
        _seed_ref(conn, "HIGH_NO_URL", media_url=None, score=1000.0)
        _seed_ref(conn, "LOW_WITH_URL", media_url="https://x/v.mp4", score=1.0)
        rows = analyzer._pending_posts(conn, 2)
    ids = [r["media_id"] for r in rows]
    assert ids == ["LOW_WITH_URL", "HIGH_NO_URL"]


def test_analyze_pending_without_url_uses_caption_only(monkeypatch, cfg, tmp_db, tmp_path: Path) -> None:
    with db_module.open_db() as conn:
        _seed_ref(conn, "M1", media_url=None, caption="only caption")

    monkeypatch.setattr(analyzer, "TMP_DIR", tmp_path)

    captured = {}

    def fake_claude(c, caption, frames):
        captured["caption"] = caption
        captured["frames"] = frames
        return {"situation": "s", "hook": "h", "punchline": "p",
                "camera_style": "cs", "cat_type": "ct", "format_tags": ["f"], "why_it_works": "w"}

    monkeypatch.setattr(analyzer, "_call_claude_for_analysis", fake_claude)

    stats = analyzer.analyze_pending(cfg, limit=5)
    assert stats["analyzed"] == 1
    assert stats["skipped_no_url"] == 1
    assert captured["frames"] == []

    with db_module.open_db() as conn:
        row = conn.execute("SELECT result_json FROM analyses WHERE media_id = 'M1'").fetchone()
        assert row is not None
        data = json.loads(row["result_json"])
        assert data["situation"] == "s"

        ref = conn.execute("SELECT analyzed FROM reference_posts WHERE media_id = 'M1'").fetchone()
        assert ref["analyzed"] == 1


def test_analyze_pending_cleans_tmp_on_failure(monkeypatch, cfg, tmp_db, tmp_path: Path) -> None:
    with db_module.open_db() as conn:
        _seed_ref(conn, "M2", media_url=None, caption="x")

    monkeypatch.setattr(analyzer, "TMP_DIR", tmp_path)

    def boom(*a, **k):
        raise RuntimeError("claude down")

    monkeypatch.setattr(analyzer, "_call_claude_for_analysis", boom)

    stats = analyzer.analyze_pending(cfg, limit=5)
    assert stats["failed"] == 1
    # tmp 以下の media_id ディレクトリは掃除されている
    assert not (tmp_path / "M2").exists()
    with db_module.open_db() as conn:
        row = conn.execute("SELECT analyzed FROM reference_posts WHERE media_id = 'M2'").fetchone()
        assert row["analyzed"] == 0  # 失敗は未分析のまま残す
