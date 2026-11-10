from __future__ import annotations

from pathlib import Path

import pytest

from app import db as db_module
from app import instagram_client as ig
from app import publisher


def _seed_generation(video_path: Path, status: str = "generated") -> int:
    with db_module.open_db() as conn:
        cur = conn.execute(
            "INSERT INTO generations(kling_prompt, caption_ja, status, video_path, created_at)"
            " VALUES ('p', 'hello #AI生成', ?, ?, '2026-01-01T00:00:00+00:00')",
            (status, str(video_path)),
        )
        return int(cur.lastrowid)


def test_publish_dry_run(cfg, tmp_db, tmp_path: Path) -> None:
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x")
    gen_id = _seed_generation(v)

    cfg_dry = cfg.__class__(**{**cfg.__dict__, "dry_run": True})
    result = publisher.publish_for(cfg_dry, gen_id)
    assert result == {"dry_run": True, "video_path": str(v), "caption": "hello #AI生成"}

    with db_module.open_db() as conn:
        row = conn.execute("SELECT status FROM generations WHERE id = ?", (gen_id,)).fetchone()
    assert row["status"] == "generated"  # dry-run では状態を変えない


def test_publish_happy_path(monkeypatch, cfg, tmp_db, tmp_path: Path) -> None:
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x")
    gen_id = _seed_generation(v)

    calls: list[str] = []
    monkeypatch.setattr(ig, "publishing_limit", lambda c: {"quota_usage": 0, "config": {"quota_total": 100}})
    monkeypatch.setattr(ig, "create_reel_container_resumable", lambda c, cap: calls.append("container") or "C1")
    monkeypatch.setattr(ig, "upload_video", lambda c, cid, p: calls.append("upload"))
    monkeypatch.setattr(ig, "wait_container", lambda c, cid: calls.append("wait"))
    monkeypatch.setattr(ig, "publish", lambda c, cid: calls.append("publish") or "IG_MEDIA_1")
    monkeypatch.setattr(ig, "get_media_permalink", lambda c, mid: "https://ig/p1")

    result = publisher.publish_for(cfg, gen_id)
    assert result["ig_media_id"] == "IG_MEDIA_1"
    assert result["permalink"] == "https://ig/p1"
    assert calls == ["container", "upload", "wait", "publish"]

    with db_module.open_db() as conn:
        p = conn.execute("SELECT * FROM posts WHERE ig_media_id = 'IG_MEDIA_1'").fetchone()
        g = conn.execute("SELECT status FROM generations WHERE id = ?", (gen_id,)).fetchone()
    assert p["generation_id"] == gen_id
    assert p["permalink"] == "https://ig/p1"
    assert g["status"] == "posted"


def test_publish_blocks_when_quota_exhausted(monkeypatch, cfg, tmp_db, tmp_path: Path) -> None:
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x")
    gen_id = _seed_generation(v)
    monkeypatch.setattr(ig, "publishing_limit", lambda c: {"quota_usage": 100, "config": {"quota_total": 100}})
    with pytest.raises(publisher.PublishError, match="quota"):
        publisher.publish_for(cfg, gen_id)


def test_refresh_metrics_updates_known_posts(monkeypatch, cfg, tmp_db, tmp_path: Path) -> None:
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x")
    gen_id = _seed_generation(v)
    with db_module.open_db() as conn:
        conn.execute(
            "INSERT INTO posts(ig_media_id, generation_id, permalink, posted_at, like_count, comments_count, metrics_updated_at)"
            " VALUES ('IG1', ?, 'p', '2026-01-01T00:00:00+00:00', 0, 0, '2026-01-01T00:00:00+00:00')",
            (gen_id,),
        )

    monkeypatch.setattr(ig, "my_media", lambda c: [
        {"id": "IG1", "like_count": 50, "comments_count": 5},
        {"id": "UNKNOWN", "like_count": 1, "comments_count": 1},
    ])
    n = publisher.refresh_metrics(cfg)
    assert n == 1
    with db_module.open_db() as conn:
        row = conn.execute("SELECT like_count, comments_count FROM posts WHERE ig_media_id='IG1'").fetchone()
    assert row["like_count"] == 50
    assert row["comments_count"] == 5
