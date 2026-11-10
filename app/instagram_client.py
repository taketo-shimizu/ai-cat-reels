"""Instagram Graph API クライアント (Facebook Login 経由)。

必要な権限 / 機能:
- instagram_basic
- instagram_content_publish
- instagram_manage_insights
- pages_show_list
- pages_read_engagement
- Feature: Instagram Public Content Access (hashtag 検索用)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

from app.config import Config

logger = logging.getLogger(__name__)

RETRY_MAX = 3
RETRY_BASE_DELAY = 2.0
CONTAINER_POLL_INTERVAL_SEC = 30
CONTAINER_POLL_MAX_SEC = 10 * 60
# ハッシュタグ経由: view_count は Business Discovery 専用 (入れると #36104 で全滅する)。
# media_product_type で REELS を識別する。
HASHTAG_MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,media_url,permalink,like_count,comments_count,timestamp"
)
# Business Discovery 経由: view_count を取れる
BUSINESS_DISCOVERY_MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,media_url,permalink,"
    "like_count,comments_count,view_count,timestamp"
)


class InstagramError(Exception):
    pass


class TokenExpiredError(InstagramError):
    pass


def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """429 / 5xx を指数バックオフで最大 RETRY_MAX 回まで再試行。
    401/403 と「アクセストークンの期限切れ」系は TokenExpiredError を上げて中止する。
    """
    kwargs.setdefault("timeout", 60)
    delay = RETRY_BASE_DELAY
    last_exc: Exception | None = None
    for attempt in range(1, RETRY_MAX + 1):
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as e:
            last_exc = e
            logger.warning("IG %s %s request error (attempt %d): %s", method, url, attempt, e)
        else:
            if resp.status_code in (401, 403):
                _raise_token_or_permission(resp)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                logger.warning(
                    "IG %s %s status=%d (attempt %d)", method, url, resp.status_code, attempt
                )
            else:
                return resp
        if attempt == RETRY_MAX:
            break
        time.sleep(delay)
        delay *= 2
    if last_exc is not None:
        raise InstagramError(f"IG request failed: {last_exc}") from last_exc
    raise InstagramError(f"IG request failed after {RETRY_MAX} attempts: {method} {url}")


def _raise_token_or_permission(resp: requests.Response) -> None:
    text = resp.text or ""
    try:
        err = (resp.json() or {}).get("error", {}) or {}
    except ValueError:
        err = {}
    code = err.get("code")
    sub = err.get("error_subcode")
    msg = err.get("message") or text[:400]
    # code 190 は access token 関連
    if code == 190 or sub in (463, 467):
        logger.error(
            "Instagram access token expired or invalid. 長期トークンを再発行してください。"
            " (code=%s subcode=%s message=%s)",
            code,
            sub,
            msg,
        )
        raise TokenExpiredError(f"Access token error: {msg}")
    raise InstagramError(f"IG {resp.status_code} {code}: {msg}")


def _get_json(cfg: Config, path: str, params: dict[str, str] | None = None) -> dict:
    url = f"{cfg.graph_base()}/{path.lstrip('/')}"
    q: dict[str, str] = {"access_token": cfg.ig_access_token}
    if params:
        q.update({k: str(v) for k, v in params.items()})
    resp = _request_with_retry("GET", url, params=q)
    if resp.status_code >= 400:
        _raise_token_or_permission(resp)
    return resp.json()


def _post_json(cfg: Config, path: str, body: dict) -> dict:
    url = f"{cfg.graph_base()}/{path.lstrip('/')}"
    body = {**body, "access_token": cfg.ig_access_token}
    resp = _request_with_retry("POST", url, data=body)
    if resp.status_code >= 400:
        _raise_token_or_permission(resp)
    return resp.json()


# ----- 収集系 -----


def search_hashtag_id(cfg: Config, tag: str) -> str:
    """ハッシュタグ名から node ID を得る。消費される 30/7日の制限対象。"""
    data = _get_json(cfg, "ig_hashtag_search", {"user_id": cfg.ig_user_id, "q": tag})
    items = data.get("data") or []
    if not items or not items[0].get("id"):
        raise InstagramError(f"hashtag_search: no id returned for '{tag}'")
    return str(items[0]["id"])


def hashtag_top_media(cfg: Config, hashtag_id: str) -> list[dict]:
    data = _get_json(
        cfg,
        f"{hashtag_id}/top_media",
        {"user_id": cfg.ig_user_id, "fields": HASHTAG_MEDIA_FIELDS},
    )
    return data.get("data") or []


def hashtag_recent_media(cfg: Config, hashtag_id: str) -> list[dict]:
    data = _get_json(
        cfg,
        f"{hashtag_id}/recent_media",
        {"user_id": cfg.ig_user_id, "fields": HASHTAG_MEDIA_FIELDS},
    )
    return data.get("data") or []


def account_media(cfg: Config, username: str) -> dict:
    """Business Discovery。相手がビジネス/クリエイターでないと空になる。"""
    fields = (
        f"business_discovery.username({username})"
        "{followers_count,media{" + BUSINESS_DISCOVERY_MEDIA_FIELDS + "}}"
    )
    data = _get_json(cfg, cfg.ig_user_id, {"fields": fields})
    bd = data.get("business_discovery") or {}
    return bd


def my_media(cfg: Config) -> list[dict]:
    """自分の投稿のいいね・コメント数等を取得。"""
    fields = "id,caption,like_count,comments_count,timestamp,permalink"
    data = _get_json(cfg, f"{cfg.ig_user_id}/media", {"fields": fields})
    return data.get("data") or []


# ----- 投稿系 -----


def publishing_limit(cfg: Config) -> dict:
    """直近24時間の投稿数と残り。"""
    data = _get_json(cfg, f"{cfg.ig_user_id}/content_publishing_limit", {"fields": "quota_usage,config"})
    items = data.get("data") or []
    return items[0] if items else {}


def create_reel_container_resumable(cfg: Config, caption: str) -> str:
    """resumable upload 用のコンテナを作成。戻り値は container_id。"""
    body = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption,
        "share_to_feed": "true",
        "is_ai_generated": "true",
    }
    data = _post_json(cfg, f"{cfg.ig_user_id}/media", body)
    container_id = data.get("id")
    if not container_id:
        raise InstagramError(f"create container: id missing in response: {data}")
    return str(container_id)


def upload_video(cfg: Config, container_id: str, path: Path) -> None:
    """ローカルの動画ファイルを rupload にアップロードする。"""
    p = Path(path)
    if not p.exists():
        raise InstagramError(f"video file not found: {p}")
    file_size = p.stat().st_size
    url = f"{cfg.rupload_base()}/{container_id}"
    headers = {
        "Authorization": f"OAuth {cfg.ig_access_token}",
        "offset": "0",
        "file_size": str(file_size),
    }
    with p.open("rb") as f:
        resp = _request_with_retry("POST", url, headers=headers, data=f)
    if resp.status_code >= 400:
        raise InstagramError(f"upload_video failed {resp.status_code}: {resp.text[:400]}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if body.get("success") is False:
        raise InstagramError(f"upload_video unsuccessful: {body}")


def wait_container(cfg: Config, container_id: str) -> None:
    """コンテナの status_code が FINISHED になるまで待つ。ERROR/EXPIRED は失敗。"""
    deadline = time.time() + CONTAINER_POLL_MAX_SEC
    while time.time() < deadline:
        data = _get_json(cfg, container_id, {"fields": "status_code,status"})
        status = data.get("status_code")
        logger.info("container %s status=%s", container_id, status)
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise InstagramError(
                f"container {container_id} status={status} detail={data.get('status')}"
            )
        time.sleep(CONTAINER_POLL_INTERVAL_SEC)
    raise InstagramError(f"container {container_id} wait timed out")


def publish(cfg: Config, container_id: str) -> str:
    data = _post_json(cfg, f"{cfg.ig_user_id}/media_publish", {"creation_id": container_id})
    media_id = data.get("id")
    if not media_id:
        raise InstagramError(f"publish: id missing in response: {data}")
    return str(media_id)


def get_media_permalink(cfg: Config, media_id: str) -> str | None:
    data = _get_json(cfg, media_id, {"fields": "permalink"})
    return data.get("permalink")
