from __future__ import annotations

import json

from app import db as db_module
from app import planner


def test_ensure_ai_label_appends() -> None:
    result = planner._ensure_ai_label("hi #cat")
    assert planner.AI_LABEL_TAG in result


def test_ensure_ai_label_noop_if_present() -> None:
    text = f"already {planner.AI_LABEL_TAG}"
    assert planner._ensure_ai_label(text) == text


def test_ensure_cat_description_prepends_when_missing() -> None:
    result = planner._ensure_cat_description("a cat on a desk")
    assert "tabby" in result.lower()
    assert "a cat on a desk" in result


def test_ensure_cat_description_noop_when_tabby_present() -> None:
    text = "a chubby brown tabby cat sitting on a desk"
    assert planner._ensure_cat_description(text) == text


def test_plan_today_inserts_generation(monkeypatch, cfg, tmp_db) -> None:
    monkeypatch.setattr(
        planner,
        "_call_claude",
        lambda c, a, o: {
            "idea_ja": "猫が洗濯機に乗る",
            "kling_prompt": "a tabby cat riding a washing machine, 9:16, 5s",
            "negative_prompt": "humans, text",
            "caption_ja": "Spin cycle vibes 🌀",
            "based_on": ["ハプニング"],
        },
    )

    gen_id = planner.plan_today(cfg)
    assert gen_id > 0
    with db_module.open_db() as conn:
        row = conn.execute("SELECT * FROM generations WHERE id = ?", (gen_id,)).fetchone()
    assert row["status"] == "planned"
    assert row["kling_prompt"].startswith("a tabby cat")
    assert planner.AI_LABEL_TAG in row["caption_ja"]
    assert json.loads(row["based_on"]) == ["ハプニング"]


def test_plan_today_forces_cat_description_when_missing(monkeypatch, cfg, tmp_db) -> None:
    monkeypatch.setattr(
        planner,
        "_call_claude",
        lambda c, a, o: {
            "idea_ja": "test",
            "kling_prompt": "a random animal doing stuff",  # tabby 未含
            "negative_prompt": None,
            "caption_ja": "Hello world",
            "based_on": [],
        },
    )
    gen_id = planner.plan_today(cfg)
    with db_module.open_db() as conn:
        row = conn.execute("SELECT kling_prompt FROM generations WHERE id = ?", (gen_id,)).fetchone()
    assert "tabby" in row["kling_prompt"].lower()
