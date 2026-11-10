"""リール投稿の一連の流れと、自分の投稿のメトリクス更新。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from app import db as db_module
from app import instagram_client as ig
from app.config import Config

logger = logging.getLogger(__name__)


class PublishError(Exception):
    pass


def _pick_generation(conn, generation_id: int | None):
    if generation_id is not None:
        row = conn.execute(
            "SELECT id, caption_ja, video_path, status FROM generations WHERE id = ?",
            (generation_id,),
        ).fetchone()
        if row is None:
            raise PublishError(f"generation_id={generation_id} not found")
        return row
    row = conn.execute(
        "SELECT id, caption_ja, video_path, status FROM generations"
        " WHERE status = 'generated' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise PublishError("No generated video. Run `generate` first.")
    return row


def publish_for(cfg: Config, generation_id: int | None = None) -> dict:
    with db_module.open_db() as conn:
        row = _pick_generation(conn, generation_id)

    gen_id = int(row["id"])
    caption = row["caption_ja"] or ""
    video_path = Path(row["video_path"]) if row["video_path"] else None
    if video_path is None or not video_path.exists():
        raise PublishError(f"video file missing for generation_id={gen_id}: {video_path}")

    if cfg.dry_run:
        logger.info("[DRY_RUN] skip publish. video=%s", video_path)
        logger.info("[DRY_RUN] caption:\n%s", caption)
        return {"dry_run": True, "video_path": str(video_path), "caption": caption}

    limit = ig.publishing_limit(cfg)
    quota_used = int(limit.get("quota_usage", 0) or 0)
    config = limit.get("config") or {}
    quota_total = int(config.get("quota_total", 100) or 100)
    if quota_used >= quota_total:
        raise PublishError(f"publishing quota exhausted: used={quota_used}/{quota_total}")
    logger.info("publishing quota: %d / %d", quota_used, quota_total)

    container_id = ig.create_reel_container_resumable(cfg, caption)
    logger.info("container created: %s", container_id)

    ig.upload_video(cfg, container_id, video_path)
    logger.info("video uploaded")

    ig.wait_container(cfg, container_id)
    media_id = ig.publish(cfg, container_id)
    permalink = ig.get_media_permalink(cfg, media_id)
    now = datetime.now(timezone.utc).isoformat()

    with db_module.open_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO posts(
                ig_media_id, generation_id, permalink, posted_at,
                like_count, comments_count, metrics_updated_at
            ) VALUES (?, ?, ?, ?, 0, 0, ?)
            """,
            (media_id, gen_id, permalink, now, now),
        )
        conn.execute("UPDATE generations SET status = 'posted' WHERE id = ?", (gen_id,))

    logger.info("published: ig_media_id=%s permalink=%s", media_id, permalink)
    return {"ig_media_id": media_id, "permalink": permalink, "generation_id": gen_id}


def refresh_metrics(cfg: Config) -> int:
    """自分の投稿のいいね・コメント数を再取得して posts を更新する。"""
    media = ig.my_media(cfg)
    if not media:
        return 0
    updates = 0
    now = datetime.now(timezone.utc).isoformat()
    with db_module.open_db() as conn:
        for m in media:
            mid = m.get("id")
            if not mid:
                continue
            cur = conn.execute(
                """
                UPDATE posts
                SET like_count = ?, comments_count = ?, metrics_updated_at = ?
                WHERE ig_media_id = ?
                """,
                (
                    int(m.get("like_count") or 0),
                    int(m.get("comments_count") or 0),
                    now,
                    mid,
                ),
            )
            if cur.rowcount > 0:
                updates += 1
    return updates
