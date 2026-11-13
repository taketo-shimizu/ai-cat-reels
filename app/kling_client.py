"""Kling API クライアント。

- JWT (HS256) 署名: iss=AccessKey, exp=now+1800s, nbf=now-5s
- POST /v1/videos/text2video でタスク作成
- GET /v1/videos/text2video/{task_id} でポーリング (task_status: submitted / processing / succeed / failed)
- succeed になったら即ダウンロード (動画URLは短時間で失効)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import jwt
import requests

from app import db as db_module
from app.config import VIDEOS_DIR, Config

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 15
POLL_MAX_SEC = 15 * 60
RETRY_MAX = 3
RETRY_BASE_DELAY = 2.0


class KlingError(Exception):
    pass


def _issue_jwt(access_key: str, secret_key: str, now: int | None = None) -> str:
    """Kling 仕様の短期 JWT を発行する (レガシー Access Key + Secret Key 方式)。"""
    n = now if now is not None else int(time.time())
    payload = {"iss": access_key, "exp": n + 1800, "nbf": n - 5}
    headers = {"alg": "HS256", "typ": "JWT"}
    return jwt.encode(payload, secret_key, algorithm="HS256", headers=headers)


def _auth_header(cfg: Config) -> dict[str, str]:
    """新方式 (単体 API Key) が設定されていればそれを使う。無ければ JWT を発行。"""
    if cfg.kling_api_key:
        return {"Authorization": f"Bearer {cfg.kling_api_key}"}
    if cfg.kling_access_key and cfg.kling_secret_key:
        token = _issue_jwt(cfg.kling_access_key, cfg.kling_secret_key)
        return {"Authorization": f"Bearer {token}"}
    raise KlingError(
        "Kling credentials not set. KLING_API_KEY or (KLING_ACCESS_KEY + KLING_SECRET_KEY) が必要です。"
    )


def _request_with_retry(
    method: str, url: str, *, headers: dict[str, str], **kwargs
) -> requests.Response:
    """429 / 5xx を指数バックオフで最大 RETRY_MAX 回まで再試行。"""
    delay = RETRY_BASE_DELAY
    last_exc: Exception | None = None
    for attempt in range(1, RETRY_MAX + 1):
        try:
            resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
        except requests.RequestException as e:
            last_exc = e
            logger.warning("Kling %s %s request error (attempt %d): %s", method, url, attempt, e)
        else:
            if resp.status_code in (401, 403):
                raise KlingError(f"Kling auth/permission error {resp.status_code}: {resp.text[:400]}")
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                logger.warning(
                    "Kling %s %s status=%d (attempt %d)", method, url, resp.status_code, attempt
                )
            else:
                return resp
        if attempt == RETRY_MAX:
            break
        time.sleep(delay)
        delay *= 2
    if last_exc is not None:
        raise KlingError(f"Kling request failed: {last_exc}") from last_exc
    raise KlingError(f"Kling request failed after {RETRY_MAX} attempts: {method} {url}")


def create_text2video_task(cfg: Config, prompt: str, negative_prompt: str | None = None) -> str:
    """text2video タスクを作成し task_id を返す。"""
    url = f"{cfg.kling_base_url}/v1/videos/text2video"
    body: dict[str, object] = {
        "model_name": cfg.kling_model,
        "prompt": prompt,
        "mode": cfg.kling_mode,
        "duration": str(cfg.kling_duration),
        "aspect_ratio": "9:16",
        "sound": cfg.kling_sound,
    }
    if negative_prompt:
        body["negative_prompt"] = negative_prompt
    resp = _request_with_retry("POST", url, headers=_auth_header(cfg), json=body)
    if resp.status_code >= 400:
        raise KlingError(f"Kling create task failed {resp.status_code}: {resp.text[:400]}")
    data = resp.json()
    task_id = (data.get("data") or {}).get("task_id")
    if not task_id:
        raise KlingError(f"Kling create task: task_id missing in response: {data}")
    logger.info("Kling task created: task_id=%s", task_id)
    return str(task_id)


def wait_task(cfg: Config, task_id: str) -> str:
    """task_status が succeed になるまでポーリング。成功時は動画URLを返す。"""
    url = f"{cfg.kling_base_url}/v1/videos/text2video/{task_id}"
    deadline = time.time() + POLL_MAX_SEC
    while time.time() < deadline:
        resp = _request_with_retry("GET", url, headers=_auth_header(cfg))
        if resp.status_code >= 400:
            raise KlingError(f"Kling poll failed {resp.status_code}: {resp.text[:400]}")
        data = (resp.json() or {}).get("data") or {}
        status = data.get("task_status")
        if status == "succeed":
            videos = ((data.get("task_result") or {}).get("videos")) or []
            if not videos or not videos[0].get("url"):
                raise KlingError(f"Kling succeed but no video url: {data}")
            video_url = videos[0]["url"]
            logger.info("Kling task succeeded: task_id=%s", task_id)
            return str(video_url)
        if status == "failed":
            msg = data.get("task_status_msg") or "unknown reason"
            raise KlingError(f"Kling task failed: task_id={task_id} msg={msg}")
        logger.info("Kling task polling: task_id=%s status=%s", task_id, status)
        time.sleep(POLL_INTERVAL_SEC)
    raise KlingError(f"Kling task timed out after {POLL_MAX_SEC}s: task_id={task_id}")


def download_video(video_url: str, task_id: str, out_dir: Path | None = None) -> Path:
    """Kling の動画URLは短時間で失効するので、succeed 直後にダウンロードする。"""
    out_dir = out_dir or VIDEOS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = out_dir / f"{today}_{task_id}.mp4"
    with requests.get(video_url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with out_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    logger.info("Downloaded Kling video: %s", out_path)
    return out_path


def _pick_generation_id(generation_id: int | None) -> int:
    with db_module.open_db() as conn:
        if generation_id is not None:
            row = conn.execute("SELECT id FROM generations WHERE id = ?", (generation_id,)).fetchone()
            if row is None:
                raise KlingError(f"generation_id={generation_id} not found")
            return int(row["id"])
        row = conn.execute(
            "SELECT id FROM generations WHERE status = 'planned' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise KlingError("No planned generation. Run `plan` first.")
        return int(row["id"])


def generate_for(cfg: Config, generation_id: int | None = None) -> int:
    """生成本体。1度だけ失敗時に同じプロンプトで再生成する。"""
    gen_id = _pick_generation_id(generation_id)
    with db_module.open_db() as conn:
        row = conn.execute(
            "SELECT kling_prompt, negative_prompt FROM generations WHERE id = ?", (gen_id,)
        ).fetchone()
        prompt = row["kling_prompt"]
        negative_prompt = row["negative_prompt"]
        conn.execute("UPDATE generations SET status = 'generating' WHERE id = ?", (gen_id,))

    attempts = 0
    last_err: Exception | None = None
    while attempts < 2:
        attempts += 1
        try:
            task_id = create_text2video_task(cfg, prompt, negative_prompt)
            with db_module.open_db() as conn:
                conn.execute(
                    "UPDATE generations SET kling_task_id = ? WHERE id = ?", (task_id, gen_id)
                )
            video_url = wait_task(cfg, task_id)
            video_path = download_video(video_url, task_id)
            with db_module.open_db() as conn:
                conn.execute(
                    "UPDATE generations SET status = 'generated', video_path = ?, error = NULL WHERE id = ?",
                    (str(video_path), gen_id),
                )
            return gen_id
        except KlingError as e:
            last_err = e
            logger.warning("Kling attempt %d failed: %s", attempts, e)

    err_text = str(last_err) if last_err else "unknown"
    with db_module.open_db() as conn:
        conn.execute(
            "UPDATE generations SET status = 'failed', error = ? WHERE id = ?",
            (err_text, gen_id),
        )
    raise KlingError(f"generation_id={gen_id} failed after retry: {err_text}")
