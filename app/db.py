"""SQLite 接続と DDL。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS hashtags (
  name TEXT PRIMARY KEY,
  hashtag_id TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reference_posts (
  media_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  caption TEXT,
  media_url TEXT,
  permalink TEXT,
  like_count INTEGER,
  comments_count INTEGER,
  view_count INTEGER,
  posted_at TEXT,
  score REAL,
  collected_at TEXT NOT NULL,
  analyzed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  media_id TEXT REFERENCES reference_posts(media_id),
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_ja TEXT,
  kling_prompt TEXT NOT NULL,
  negative_prompt TEXT,
  caption_ja TEXT,
  based_on TEXT,
  kling_task_id TEXT,
  status TEXT NOT NULL,
  video_path TEXT,
  error TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
  ig_media_id TEXT PRIMARY KEY,
  generation_id INTEGER REFERENCES generations(id),
  permalink TEXT,
  posted_at TEXT NOT NULL,
  like_count INTEGER,
  comments_count INTEGER,
  metrics_updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_reference_posts_analyzed ON reference_posts(analyzed);
CREATE INDEX IF NOT EXISTS idx_reference_posts_score ON reference_posts(score DESC);
CREATE INDEX IF NOT EXISTS idx_generations_status ON generations(status);
CREATE INDEX IF NOT EXISTS idx_posts_posted_at ON posts(posted_at);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def open_db(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
