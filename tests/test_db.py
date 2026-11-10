from __future__ import annotations

from app import db as db_module


def test_init_creates_all_tables(tmp_db) -> None:
    with db_module.open_db(tmp_db) as conn:
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert {"hashtags", "reference_posts", "analyses", "generations", "posts"} <= names


def test_rollback_on_error(tmp_db) -> None:
    try:
        with db_module.open_db(tmp_db) as conn:
            conn.execute(
                "INSERT INTO hashtags(name, hashtag_id, fetched_at) VALUES ('x', '1', '2026-01-01')"
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with db_module.open_db(tmp_db) as conn:
        row = conn.execute("SELECT 1 FROM hashtags WHERE name = 'x'").fetchone()
        assert row is None
