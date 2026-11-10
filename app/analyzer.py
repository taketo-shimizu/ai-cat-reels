"""参考動画の分析。ffmpeg で 4 枚のコマを抜き、Claude の vision で JSON を得る。"""

from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests
from anthropic import Anthropic

from app import db as db_module
from app.config import TMP_DIR, Config

logger = logging.getLogger(__name__)

FRAME_COUNT = 4
ANALYZE_SYSTEM_PROMPT = (
    "あなたは Instagram のリール動画の構成分析アシスタントです。"
    "与えられた画像コマとキャプションから、動画の構成・ネタ・伸びた理由を日本語で分析し、"
    "必ず以下のキーだけを持つ JSON のみを返してください。前置きや後置きは禁止です。\n"
    "- situation: 場面の一文説明\n"
    "- hook: 最初の1秒のつかみ\n"
    "- punchline: オチ\n"
    "- camera_style: 例) 防犯カメラ風 / スマホ風 / 固定ショット\n"
    "- cat_type: 猫の種類・毛色\n"
    "- format_tags: 配列 (例: 擬人化, ハプニング, おあずけ)\n"
    "- why_it_works: 伸びた理由の推測\n"
)


def _download_to_tmp(url: str, dest: Path) -> Path:
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    return dest


def _video_duration_seconds(video: Path) -> float:
    """ffprobe で尺を取る。失敗したら 10 秒とみなす。"""
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        return float(out.strip() or b"10")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return 10.0


def extract_frames(video: Path, out_dir: Path, count: int = FRAME_COUNT) -> list[Path]:
    """等間隔に count 枚の JPEG を抜く。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = _video_duration_seconds(video)
    step = max(duration / (count + 1), 0.5)
    frames: list[Path] = []
    for i in range(1, count + 1):
        at = step * i
        out = out_dir / f"{video.stem}_f{i}.jpg"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{at:.2f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(out),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if out.exists():
            frames.append(out)
    return frames


def _b64_jpeg(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("ascii")


def _parse_json_strict(text: str) -> dict:
    """モデル出力から JSON オブジェクトを抜き出す。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 中括弧の範囲でフォールバック
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def _call_claude_for_analysis(cfg: Config, caption: str | None, frames: list[Path]) -> dict:
    client = Anthropic(api_key=cfg.anthropic_api_key)
    content: list[dict] = []
    for p in frames:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": _b64_jpeg(p),
                },
            }
        )
    content.append(
        {
            "type": "text",
            "text": "キャプション:\n" + (caption or "(なし)") + "\n\nJSON を返してください。",
        }
    )
    resp = client.messages.create(
        model=cfg.claude_model,
        max_tokens=1024,
        system=ANALYZE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
    return _parse_json_strict(text)


def _pending_posts(conn, limit: int) -> list:
    """未分析の参考投稿を、コマ抽出できるもの (media_url あり) を優先して返す。
    同列ではスコア順。URL ありが枯れたら URL なしも caption-only で処理する。
    """
    return conn.execute(
        """
        SELECT media_id, caption, media_url FROM reference_posts
        WHERE analyzed = 0
        ORDER BY (CASE WHEN media_url IS NULL OR media_url = '' THEN 1 ELSE 0 END),
                 score DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def analyze_pending(cfg: Config, limit: int = 10) -> dict:
    stats = {"analyzed": 0, "failed": 0, "skipped_no_url": 0}
    with db_module.open_db() as conn:
        rows = _pending_posts(conn, limit)

    for row in rows:
        media_id = row["media_id"]
        caption = row["caption"]
        media_url = row["media_url"]
        work_dir = TMP_DIR / media_id
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            frames: list[Path] = []
            if media_url:
                video_path = work_dir / "video.mp4"
                try:
                    _download_to_tmp(media_url, video_path)
                    frames = extract_frames(video_path, work_dir)
                except Exception as e:
                    logger.warning("frame extraction failed for %s: %s (fallback to caption only)", media_id, e)
                    frames = []
            else:
                stats["skipped_no_url"] += 1

            result = _call_claude_for_analysis(cfg, caption, frames)
            now = datetime.now(timezone.utc).isoformat()
            with db_module.open_db() as conn:
                conn.execute(
                    "INSERT INTO analyses(media_id, result_json, created_at) VALUES (?, ?, ?)",
                    (media_id, json.dumps(result, ensure_ascii=False), now),
                )
                conn.execute(
                    "UPDATE reference_posts SET analyzed = 1 WHERE media_id = ?", (media_id,)
                )
            stats["analyzed"] += 1
        except Exception as e:
            logger.exception("analyze failed media_id=%s: %s", media_id, e)
            stats["failed"] += 1
        finally:
            # tmp は必ず削除
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
    return stats
