from __future__ import annotations

from pathlib import Path

import pytest

from app import instagram_client as ig


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or ""

    def json(self) -> dict:
        return self._payload


def _stub_response(monkeypatch: pytest.MonkeyPatch, resp: FakeResponse) -> dict:
    captured: dict = {}

    def fake(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["kwargs"] = kwargs
        return resp

    monkeypatch.setattr(ig, "_request_with_retry", fake)
    return captured


def test_search_hashtag_id(monkeypatch, cfg) -> None:
    captured = _stub_response(monkeypatch, FakeResponse(200, {"data": [{"id": "H1"}]}))
    hid = ig.search_hashtag_id(cfg, "aicat")
    assert hid == "H1"
    assert captured["url"].endswith("/v23.0/ig_hashtag_search")
    assert captured["kwargs"]["params"]["q"] == "aicat"
    assert captured["kwargs"]["params"]["user_id"] == "123456"


def test_hashtag_top_media(monkeypatch, cfg) -> None:
    captured = _stub_response(monkeypatch, FakeResponse(200, {"data": [{"id": "M1"}]}))
    items = ig.hashtag_top_media(cfg, "H1")
    assert items == [{"id": "M1"}]
    assert "/H1/top_media" in captured["url"]
    assert "media_type" in captured["kwargs"]["params"]["fields"]


def test_account_media(monkeypatch, cfg) -> None:
    captured = _stub_response(
        monkeypatch,
        FakeResponse(200, {"business_discovery": {"followers_count": 1000, "media": {"data": [{"id": "M1"}]}}}),
    )
    bd = ig.account_media(cfg, "someuser")
    assert bd["followers_count"] == 1000
    assert "business_discovery.username(someuser)" in captured["kwargs"]["params"]["fields"]


def test_create_reel_container_includes_is_ai_generated(monkeypatch, cfg) -> None:
    captured = _stub_response(monkeypatch, FakeResponse(200, {"id": "C1"}))
    cid = ig.create_reel_container_resumable(cfg, "hello #AI生成")
    assert cid == "C1"
    assert captured["method"] == "POST"
    assert captured["kwargs"]["data"]["media_type"] == "REELS"
    assert captured["kwargs"]["data"]["upload_type"] == "resumable"
    assert captured["kwargs"]["data"]["is_ai_generated"] == "true"
    assert captured["kwargs"]["data"]["share_to_feed"] == "true"


def test_upload_video_sets_offset_and_file_size(monkeypatch, cfg, tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"hello")
    captured = _stub_response(monkeypatch, FakeResponse(200, {"success": True}))
    ig.upload_video(cfg, "C1", video)
    assert "rupload.facebook.com" in captured["url"]
    assert "/C1" in captured["url"]
    headers = captured["kwargs"]["headers"]
    assert headers["Authorization"] == "OAuth TEST_TOKEN"
    assert headers["offset"] == "0"
    assert headers["file_size"] == "5"


def test_wait_container_success(monkeypatch, cfg) -> None:
    responses = [
        FakeResponse(200, {"status_code": "IN_PROGRESS"}),
        FakeResponse(200, {"status_code": "FINISHED"}),
    ]
    monkeypatch.setattr(ig, "_request_with_retry", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(ig.time, "sleep", lambda s: None)
    monkeypatch.setattr(ig, "CONTAINER_POLL_INTERVAL_SEC", 0)
    ig.wait_container(cfg, "C1")


def test_wait_container_error(monkeypatch, cfg) -> None:
    monkeypatch.setattr(
        ig,
        "_request_with_retry",
        lambda *a, **k: FakeResponse(200, {"status_code": "ERROR", "status": "boom"}),
    )
    monkeypatch.setattr(ig.time, "sleep", lambda s: None)
    with pytest.raises(ig.InstagramError):
        ig.wait_container(cfg, "C1")


def test_publish(monkeypatch, cfg) -> None:
    captured = _stub_response(monkeypatch, FakeResponse(200, {"id": "IG_MEDIA_1"}))
    assert ig.publish(cfg, "C1") == "IG_MEDIA_1"
    assert captured["kwargs"]["data"]["creation_id"] == "C1"
    assert captured["url"].endswith("/v23.0/123456/media_publish")


def test_token_expired_error_raises(monkeypatch, cfg) -> None:
    class Resp(FakeResponse):
        def __init__(self) -> None:
            super().__init__(401, {"error": {"code": 190, "message": "token expired"}}, "")

    monkeypatch.setattr(ig.requests, "request", lambda *a, **k: Resp())
    with pytest.raises(ig.TokenExpiredError):
        ig._get_json(cfg, "me")
