from __future__ import annotations

from pathlib import Path

import jwt
import pytest

from app import db as db_module
from app import kling_client


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = text or ""

    def json(self) -> dict:
        return self._payload

    def iter_content(self, chunk_size: int = 1024):  # pragma: no cover - not used here
        return iter([])

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_issue_jwt_shape() -> None:
    token = kling_client._issue_jwt("ak", "sk", now=1_700_000_000)
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "HS256"
    assert header["typ"] == "JWT"
    decoded = jwt.decode(
        token, "sk", algorithms=["HS256"], options={"verify_exp": False, "verify_nbf": False}
    )
    assert decoded["iss"] == "ak"
    assert decoded["exp"] == 1_700_000_000 + 1800
    assert decoded["nbf"] == 1_700_000_000 - 5


def test_auth_header_prefers_api_key(cfg) -> None:
    cfg_new = cfg.__class__(**{**cfg.__dict__, "kling_api_key": "sk-new-key"})
    header = kling_client._auth_header(cfg_new)
    assert header == {"Authorization": "Bearer sk-new-key"}


def test_auth_header_falls_back_to_jwt(cfg) -> None:
    header = kling_client._auth_header(cfg)  # fixture has AK/SK only
    assert header["Authorization"].startswith("Bearer ")
    token = header["Authorization"].removeprefix("Bearer ")
    decoded = jwt.decode(
        token, "SK", algorithms=["HS256"], options={"verify_exp": False, "verify_nbf": False}
    )
    assert decoded["iss"] == "AK"


def test_auth_header_raises_without_credentials(cfg) -> None:
    empty = cfg.__class__(**{**cfg.__dict__, "kling_access_key": "", "kling_secret_key": ""})
    with pytest.raises(kling_client.KlingError, match="credentials"):
        kling_client._auth_header(empty)


def test_create_text2video_task(monkeypatch: pytest.MonkeyPatch, cfg) -> None:
    captured: dict = {}

    def fake_request(method, url, *, headers, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = kwargs.get("json")
        return FakeResponse(200, {"data": {"task_id": "T1"}})

    monkeypatch.setattr(kling_client, "_request_with_retry", fake_request)

    task_id = kling_client.create_text2video_task(cfg, "a cat", "no humans")
    assert task_id == "T1"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v1/videos/text2video")
    assert captured["json"]["model_name"] == "kling-v2-6"
    assert captured["json"]["aspect_ratio"] == "9:16"
    assert captured["json"]["duration"] == "5"
    assert captured["json"]["sound"] == "on"
    assert captured["json"]["negative_prompt"] == "no humans"
    assert captured["headers"]["Authorization"].startswith("Bearer ")


def test_wait_task_success(monkeypatch: pytest.MonkeyPatch, cfg) -> None:
    responses = [
        FakeResponse(200, {"data": {"task_status": "submitted"}}),
        FakeResponse(200, {"data": {"task_status": "processing"}}),
        FakeResponse(200, {"data": {
            "task_status": "succeed",
            "task_result": {"videos": [{"url": "https://cdn/v.mp4"}]},
        }}),
    ]
    monkeypatch.setattr(kling_client, "_request_with_retry", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(kling_client.time, "sleep", lambda s: None)
    monkeypatch.setattr(kling_client, "POLL_INTERVAL_SEC", 0)

    url = kling_client.wait_task(cfg, "T1")
    assert url == "https://cdn/v.mp4"


def test_wait_task_failed(monkeypatch: pytest.MonkeyPatch, cfg) -> None:
    monkeypatch.setattr(
        kling_client,
        "_request_with_retry",
        lambda *a, **k: FakeResponse(200, {"data": {"task_status": "failed", "task_status_msg": "oops"}}),
    )
    monkeypatch.setattr(kling_client.time, "sleep", lambda s: None)
    with pytest.raises(kling_client.KlingError, match="oops"):
        kling_client.wait_task(cfg, "T1")


def test_download_video(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class StreamResp(FakeResponse):
        def iter_content(self, chunk_size: int = 1024):
            return iter([b"abc", b"def"])

    monkeypatch.setattr(kling_client.requests, "get", lambda *a, **k: StreamResp(200))
    out = kling_client.download_video("https://x/v.mp4", "T1", tmp_path)
    assert out.exists()
    assert out.read_bytes() == b"abcdef"


def test_generate_for_success(monkeypatch: pytest.MonkeyPatch, cfg, tmp_db, tmp_path: Path) -> None:
    with db_module.open_db() as conn:
        cur = conn.execute(
            "INSERT INTO generations(kling_prompt, negative_prompt, status, created_at)"
            " VALUES (?, ?, 'planned', '2026-01-01T00:00:00+00:00')",
            ("prompt", "neg"),
        )
        gen_id = int(cur.lastrowid)

    monkeypatch.setattr(kling_client, "VIDEOS_DIR", tmp_path)
    monkeypatch.setattr(kling_client, "create_text2video_task", lambda *a, **k: "T1")
    monkeypatch.setattr(kling_client, "wait_task", lambda *a, **k: "https://cdn/v.mp4")
    monkeypatch.setattr(
        kling_client, "download_video", lambda *a, **k: tmp_path / "out.mp4"
    )
    (tmp_path / "out.mp4").write_bytes(b"x")

    returned = kling_client.generate_for(cfg, gen_id)
    assert returned == gen_id
    with db_module.open_db() as conn:
        row = conn.execute("SELECT status, video_path FROM generations WHERE id = ?", (gen_id,)).fetchone()
    assert row["status"] == "generated"
    assert row["video_path"].endswith("out.mp4")


def test_generate_for_retries_then_fails(monkeypatch: pytest.MonkeyPatch, cfg, tmp_db) -> None:
    with db_module.open_db() as conn:
        cur = conn.execute(
            "INSERT INTO generations(kling_prompt, status, created_at)"
            " VALUES ('p', 'planned', '2026-01-01T00:00:00+00:00')"
        )
        gen_id = int(cur.lastrowid)

    def boom(*a, **k):
        raise kling_client.KlingError("down")

    monkeypatch.setattr(kling_client, "create_text2video_task", boom)
    with pytest.raises(kling_client.KlingError):
        kling_client.generate_for(cfg, gen_id)

    with db_module.open_db() as conn:
        row = conn.execute("SELECT status, error FROM generations WHERE id = ?", (gen_id,)).fetchone()
    assert row["status"] == "failed"
    assert "down" in row["error"]
