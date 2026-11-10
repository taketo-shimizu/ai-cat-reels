"""人気動画の収集とスコアリング。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app import db as db_module
from app import instagram_client as ig
from app.config import ACCOUNTS_FILE, HASHTAGS_FILE, Config, read_lines

logger = logging.getLogger(__name__)


def _parse_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_since(ts: str | None, now: datetime) -> float:
    d = _parse_timestamp(ts)
    if d is None:
        return 1.0
    delta = (now - d).total_seconds() / 3600.0
    return max(delta, 1.0)


def _score(like: int, comments: int, hours: float) -> float:
    return (like + comments * 3) / hours


def _view_score(view: int | None, hours: float) -> float | None:
    if view is None:
        return None
    return view / hours


def _is_reel(media: dict) -> bool:
    """REELS だけを拾う。
    - media_type == VIDEO: 動画
    - media_product_type == REELS: 通常の FEED 動画や AD を除外
    hashtag top_media は Reels を返さないので、collector は recent_media を使う前提。
    """
    return (
        media.get("media_type") == "VIDEO"
        and media.get("media_product_type") == "REELS"
    )


def _get_cached_hashtag_id(conn, name: str) -> str | None:
    row = conn.execute("SELECT hashtag_id FROM hashtags WHERE name = ?", (name,)).fetchone()
    return row["hashtag_id"] if row else None


def _cache_hashtag_id(conn, name: str, hashtag_id: str, when: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO hashtags(name, hashtag_id, fetched_at) VALUES (?, ?, ?)",
        (name, hashtag_id, when),
    )


def _already_known(conn, media_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM reference_posts WHERE media_id = ?", (media_id,)).fetchone()
    return row is not None


def _insert_reference(conn, media: dict, source: str, collected_at: str) -> None:
    now = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    hours = _hours_since(media.get("timestamp"), now)
    like = int(media.get("like_count") or 0)
    comments = int(media.get("comments_count") or 0)
    view = media.get("view_count")
    view_int = int(view) if view is not None else None
    conn.execute(
        """
        INSERT OR IGNORE INTO reference_posts(
            media_id, source, caption, media_url, permalink,
            like_count, comments_count, view_count, posted_at, score, collected_at, analyzed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            media["id"],
            source,
            media.get("caption"),
            media.get("media_url"),
            media.get("permalink"),
            like,
            comments,
            view_int,
            media.get("timestamp"),
            _score(like, comments, hours),
            collected_at,
        ),
    )


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def collect(cfg: Config) -> dict:
    """ハッシュタグ・監視アカウントから動画を集めて reference_posts に保存。"""
    now_iso = _iso_now()
    hashtags = read_lines(HASHTAGS_FILE)
    accounts = read_lines(ACCOUNTS_FILE)

    stats = {"hashtag_hits": 0, "account_hits": 0, "skipped_non_reel": 0, "skipped_duplicate": 0}

    with db_module.open_db() as conn:
        for tag in hashtags:
            try:
                hid = _get_cached_hashtag_id(conn, tag)
                if hid is None:
                    hid = ig.search_hashtag_id(cfg, tag)
                    _cache_hashtag_id(conn, tag, hid, now_iso)
                    logger.info("cached hashtag_id: %s=%s", tag, hid)
                # top_media は仕様上 Reels を返さない (FEED のみ) ため recent_media を使う。
                media_list = ig.hashtag_recent_media(cfg, hid)
            except ig.InstagramError as e:
                logger.warning("hashtag %s failed: %s", tag, e)
                continue
            for m in media_list:
                if not _is_reel(m):
                    stats["skipped_non_reel"] += 1
                    continue
                if _already_known(conn, m["id"]):
                    stats["skipped_duplicate"] += 1
                    continue
                _insert_reference(conn, m, f"hashtag:{tag}", now_iso)
                stats["hashtag_hits"] += 1

        for username in accounts:
            try:
                bd = ig.account_media(cfg, username)
            except ig.InstagramError as e:
                logger.warning("account %s failed: %s", username, e)
                continue
            media_data = (bd.get("media") or {}).get("data") or []
            for m in media_data:
                if not _is_reel(m):
                    stats["skipped_non_reel"] += 1
                    continue
                if _already_known(conn, m["id"]):
                    stats["skipped_duplicate"] += 1
                    continue
                _insert_reference(conn, m, f"account:{username}", now_iso)
                stats["account_hits"] += 1

    return stats
