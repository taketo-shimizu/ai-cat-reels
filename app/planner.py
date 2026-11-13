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
# 登場する猫の外見指定。kling_prompt に必ず埋め込む。サイズはシーンに応じて可変。
CAT_DESCRIPTION = (
    "a cute chubby brown tabby cat (round face, warm brown fur with classic tabby stripes). "
    "The cat's size relative to the scene can scale for comedic surreal effect "
    "(e.g., kitten-sized, human-sized, or massive like a bus) while keeping the chubby proportions"
)
# Instagram キャプションの言語
CAPTION_LANGUAGE = "English"
# AI 生成であることを示すハッシュタグ (キャプション言語に合わせる)
AI_LABEL_TAG = "#AIGenerated"
# クリエイティブの方向性 (奇想天外・シュール路線 / 都市×人間ギャップ)
CREATIVE_TONE = (
    "Aim for SURREAL / ABSURD / OUTLANDISH premises that make viewers do a double-take. "
    "The humor comes from the deadpan execution of an impossible premise set in a grounded, realistic "
    "URBAN SETTING (Tokyo streets, train stations, commuter trains, office buildings, convenience stores, "
    "apartment interiors, izakaya, business districts) with REAL HUMAN CHARACTERS interacting with the cat "
    "as if the surreal situation is completely ordinary. "
    "The comedy comes from the contrast between normal human everyday life and the absurd cat scenario. "
    "AVOID: nature settings (ocean, forest, space, underwater, dream worlds, abstract backgrounds). "
    "Reference register (do NOT copy, only match the vibe — all involve URBAN scenes + HUMANS): "
    "a bus-sized giant cat acting as a taxi, picking up a tired salaryman outside Shinbashi station after work "
    "and carrying him home on its back while cars honk around it; "
    "a giant cat sitting at a boardroom table in a suit while tiny human colleagues present PowerPoint slides "
    "projected onto its massive furry belly; "
    "a cat conductor in a Yamanote line uniform punching tickets of crammed human passengers during morning rush; "
    "a cat ramen chef running a bustling sarariiman izakaya counter at midnight, pouring beer and cooking gyoza "
    "for a line of exhausted office workers; "
    "a giant cat stuck in the middle of Shibuya scramble crossing calmly washing its paw while humans in suits "
    "walk around it, nobody reacting."
)
# --------------------------------------------------------------------

PLAN_SYSTEM_PROMPT = (
    "You are a creative director for an Instagram Reels channel featuring surreal AI cat videos."
    " Produce ONE brand new AI cat video idea with a Kling text-to-video prompt. No references are given — invent freely."
    " Hard rules:\n"
    f" - {CREATIVE_TONE}\n"
    " - Avoid ideas similar to the user's own past posts (list provided below).\n"
    " - 9:16 vertical, 10 seconds, 1 or 2 clear movements.\n"
    f" - The cat MUST be: {CAT_DESCRIPTION}. Always embed this description in kling_prompt.\n"
    " - The scene must include AMBIENT SOUND cues implied by the environment (footsteps, hum, wind, etc.) so Kling can add audio naturally.\n"
    f" - Instagram caption and hashtags MUST be in {CAPTION_LANGUAGE}.\n"
    f" - The caption MUST end with {AI_LABEL_TAG}.\n"
    " Output format: a JSON object with ONLY these keys (no prose before/after):\n"
    "  - idea_ja: short idea summary in Japanese (internal record)\n"
    "  - kling_prompt: English Kling text-to-video prompt (includes cat description + ambient sound cues)\n"
    "  - negative_prompt: English negative prompt (optional)\n"
    f"  - caption_ja: Instagram caption in {CAPTION_LANGUAGE} with hashtags, within 2200 chars, ending with {AI_LABEL_TAG}\n"
    "  - based_on: array of short tags describing the surreal angle (e.g., 'giant-scale', 'absurd-job', 'fantasy-setting')\n"
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


def _call_claude(cfg: Config, own_posts: list[dict]) -> dict:
    user_text = (
        "## 自分の過去30日の投稿 (重複回避のため参照)\n"
        + json.dumps(own_posts, ensure_ascii=False, indent=2)
        + "\n\n上記と被らない、奇想天外な 1 件を JSON で返してください。"
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
        own = _recent_own_posts(conn)

    plan = _call_claude(cfg, own)
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
