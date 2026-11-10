"""直近の分析・自分の投稿から、今日のネタと Kling プロンプトを 1 件生成する。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from anthropic import Anthropic

from app import db as db_module
from app.config import Config

logger = logging.getLogger(__name__)

# ----- 動画スタイル定数 (ここを編集するだけでテイストを変えられる) -----
# 登場する猫の外見指定。kling_prompt に必ず埋め込む。
CAT_DESCRIPTION = "a small chubby cute brown tabby cat (slightly plump, round face, warm brown fur with classic tabby stripes)"
# Instagram キャプションの言語
CAPTION_LANGUAGE = "English"
# AI 生成であることを示すハッシュタグ (キャプション言語に合わせる)
AI_LABEL_TAG = "#AIGenerated"
# --------------------------------------------------------------------

PLAN_SYSTEM_PROMPT = (
    "You are an assistant that plans Instagram Reels content."
    " Learn only the 'pattern' of posts that went viral, and produce ONE new AI cat video idea with a Kling prompt."
    " Hard rules:\n"
    " - Learn only the pattern (hook type, format tags, pacing). Do NOT reproduce the composition, story, or characters of any specific reference post.\n"
    " - Avoid ideas similar to the user's own past posts.\n"
    " - 9:16, 5 seconds, 1 or 2 movements maximum.\n"
    f" - The cat in the video MUST be: {CAT_DESCRIPTION}. Always include this description in kling_prompt.\n"
    f" - Instagram caption and hashtags MUST be in {CAPTION_LANGUAGE}.\n"
    f" - The caption MUST end with {AI_LABEL_TAG}.\n"
    " Output format: a JSON object with ONLY these keys (no prose before/after):\n"
    "  - idea_ja: short idea summary (keep in Japanese for internal records)\n"
    "  - kling_prompt: English Kling text-to-video prompt (must include the cat description)\n"
    "  - negative_prompt: English negative prompt (optional)\n"
    f"  - caption_ja: Instagram caption in {CAPTION_LANGUAGE} with hashtags, within 2200 chars, ending with {AI_LABEL_TAG}\n"
    "  - based_on: array of format_tags referenced\n"
)


def _parse_json_strict(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def _recent_analyses(conn, days: int = 14, limit: int = 20) -> list[dict]:
    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT a.result_json, r.score
        FROM analyses a
        JOIN reference_posts r ON r.media_id = a.media_id
        WHERE a.created_at >= ?
        ORDER BY r.score DESC
        LIMIT ?
        """,
        (threshold, limit),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            out.append(json.loads(row["result_json"]))
        except json.JSONDecodeError:
            continue
    return out


def _recent_own_posts(conn, days: int = 30, limit: int = 30) -> list[dict]:
    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT g.idea_ja, g.kling_prompt, g.based_on, p.like_count, p.comments_count
        FROM posts p
        JOIN generations g ON g.id = p.generation_id
        WHERE p.posted_at >= ?
        ORDER BY p.posted_at DESC
        LIMIT ?
        """,
        (threshold, limit),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        out.append(
            {
                "idea_ja": row["idea_ja"],
                "kling_prompt": row["kling_prompt"],
                "based_on": json.loads(row["based_on"]) if row["based_on"] else [],
                "like_count": row["like_count"],
                "comments_count": row["comments_count"],
            }
        )
    return out


def _ensure_ai_label(caption: str | None) -> str:
    base = (caption or "").rstrip()
    if AI_LABEL_TAG not in base:
        base = (base + "\n" + AI_LABEL_TAG).lstrip()
    return base


def _ensure_cat_description(prompt: str | None) -> str:
    """Kling prompt に猫の指定が入っていなければ先頭に挿入する。保険。"""
    base = (prompt or "").strip()
    # 同一フレーズまたは主要キーワード (tabby) が含まれていればそのまま
    if "tabby" in base.lower() or CAT_DESCRIPTION in base:
        return base
    return f"{CAT_DESCRIPTION}. {base}"


def _call_claude(cfg: Config, analyses: list[dict], own_posts: list[dict]) -> dict:
    user_text = (
        "## 直近14日の伸びたリール分析 (上位)\n"
        + json.dumps(analyses, ensure_ascii=False, indent=2)
        + "\n\n## 自分の過去30日の投稿\n"
        + json.dumps(own_posts, ensure_ascii=False, indent=2)
        + "\n\n以上をふまえて今日の 1 件を JSON で返してください。"
    )
    client = Anthropic(api_key=cfg.anthropic_api_key)
    resp = client.messages.create(
        model=cfg.claude_model,
        max_tokens=1500,
        system=PLAN_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_text}],
    )
    text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
    return _parse_json_strict(text)


def plan_today(cfg: Config) -> int:
    with db_module.open_db() as conn:
        analyses = _recent_analyses(conn)
        own = _recent_own_posts(conn)

    plan = _call_claude(cfg, analyses, own)
    idea = plan.get("idea_ja")
    kling_prompt = plan.get("kling_prompt")
    if not kling_prompt:
        raise ValueError("planner: kling_prompt missing in Claude output")
    kling_prompt = _ensure_cat_description(kling_prompt)
    negative = plan.get("negative_prompt")
    caption = _ensure_ai_label(plan.get("caption_ja"))
    based_on = plan.get("based_on") or []
    if not isinstance(based_on, list):
        based_on = [str(based_on)]

    now = datetime.now(timezone.utc).isoformat()
    with db_module.open_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO generations(
                idea_ja, kling_prompt, negative_prompt, caption_ja, based_on, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'planned', ?)
            """,
            (
                idea,
                kling_prompt,
                negative,
                caption,
                json.dumps(based_on, ensure_ascii=False),
                now,
            ),
        )
        generation_id = int(cur.lastrowid)

    logger.info(
        "planned generation_id=%s idea=%s", generation_id, (idea or "")[:60]
    )
    return generation_id
