from __future__ import annotations

from pathlib import Path

import pytest

from app import collector
from app import config as cfg_module
from app import db as db_module
from app import instagram_client as ig


def _write_config_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hashtags: list[str], accounts: list[str]) -> None:
    hf = tmp_path / "hashtags.txt"
    af = tmp_path / "accounts.txt"
    hf.write_text("\n".join(hashtags), encoding="utf-8")
    af.write_text("\n".join(accounts), encoding="utf-8")
    monkeypatch.setattr(collector, "HASHTAGS_FILE", hf)
    monkeypatch.setattr(collector, "ACCOUNTS_FILE", af)


def test_collect_dedupes_and_scores(monkeypatch, cfg, tmp_db, tmp_path: Path) -> None:
    _write_config_files(monkeypatch, tmp_path, ["aicat"], ["someone"])
    monkeypatch.setattr(collector.ig, "search_hashtag_id", lambda c, t: "HID")
    monkeypatch.setattr(
        collector.ig,
        "hashtag_recent_media",
        lambda c, h: [
            {
                "id": "M1",
                "media_type": "VIDEO",
                "media_product_type": "REELS",
                "caption": "a",
                "media_url": "https://x/1",
                "permalink": "p1",
                "like_count": 100,
                "comments_count": 10,
                "view_count": None,
                "timestamp": "2026-01-01T00:00:00+0000",
            },
            {"id": "M2", "media_type": "IMAGE", "media_product_type": "FEED"},
            {"id": "M4", "media_type": "VIDEO", "media_product_type": "FEED"},  # FEED動画はReelsでないので除外
        ],
    )
    monkeypatch.setattr(
        collector.ig,
        "account_media",
        lambda c, u: {"media": {"data": [{
            "id": "M3", "media_type": "VIDEO", "media_product_type": "REELS",
            "like_count": 5, "comments_count": 1,
            "view_count": 1000, "timestamp": "2026-01-01T00:00:00+0000",
        }]}},
    )

    stats = collector.collect(cfg)
    assert stats["hashtag_hits"] == 1
    assert stats["account_hits"] == 1
    assert stats["skipped_non_reel"] == 2  # M2(IMAGE) と M4(VIDEO/FEED)

    with db_module.open_db() as conn:
        rows = {r["media_id"]: dict(r) for r in conn.execute("SELECT * FROM reference_posts")}
        assert set(rows) == {"M1", "M3"}
        assert rows["M1"]["source"] == "hashtag:aicat"
        assert rows["M3"]["source"] == "account:someone"
        assert rows["M1"]["score"] > 0

    # 二回目: 重複はスキップ
    stats2 = collector.collect(cfg)
    assert stats2["hashtag_hits"] == 0
    assert stats2["account_hits"] == 0
    assert stats2["skipped_duplicate"] >= 2


def test_collect_caches_hashtag_id(monkeypatch, cfg, tmp_db, tmp_path: Path) -> None:
    _write_config_files(monkeypatch, tmp_path, ["aicat"], [])
    calls = {"n": 0}

    def search(c, t):
        calls["n"] += 1
        return "HID"

    monkeypatch.setattr(collector.ig, "search_hashtag_id", search)
    monkeypatch.setattr(collector.ig, "hashtag_recent_media", lambda c, h: [])

    collector.collect(cfg)
    collector.collect(cfg)
    assert calls["n"] == 1  # 2回目はキャッシュで API 呼ばない


def test_score_formula() -> None:
    # (like + comments*3) / hours
    assert collector._score(100, 10, 2.0) == pytest.approx((100 + 30) / 2.0)
    assert collector._score(0, 0, 1.0) == 0.0
