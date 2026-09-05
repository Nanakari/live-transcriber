from __future__ import annotations

import json

import pytest

from app.analysis import gemini_client
from app.analysis.gemini_client import GeminiClient
from app.utils import AppError


class FakeLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def write(self, message: str) -> None:
        self.messages.append(message)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


def _success_response(value: str) -> FakeResponse:
    return FakeResponse(
        200,
        {
            "candidates": [
                {"content": {"parts": [{"text": json.dumps({"value": value})}]}}
            ]
        },
    )


def _client(monkeypatch: pytest.MonkeyPatch, *, max_retries: int = 2) -> tuple[GeminiClient, FakeLogger]:
    monkeypatch.setenv("TEST_GEMINI_KEY", "secret")
    logger = FakeLogger()
    return GeminiClient(
        model="gemini-3.5-flash-lite",
        fallback_model="gemini-3.1-flash-lite",
        api_key_env="TEST_GEMINI_KEY",
        temperature=0.2,
        max_retries=max_retries,
        retry_backoff_seconds=0,
        request_timeout_seconds=1,
        request_interval_seconds=0,
        logger=logger,  # type: ignore[arg-type]
    ), logger


def test_quota_error_switches_to_fallback_and_stays_there(monkeypatch: pytest.MonkeyPatch) -> None:
    client, logger = _client(monkeypatch)
    calls: list[tuple[str, dict]] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        calls.append((url, kwargs["json"]))  # type: ignore[index]
        if "gemini-3.5-flash-lite" in url:
            return FakeResponse(429, text='{"error":{"status":"RESOURCE_EXHAUSTED"}}')
        return _success_response("fallback")

    monkeypatch.setattr(gemini_client.requests, "post", fake_post)

    assert json.loads(client.generate_json_text("one", system_prompt="system"))["value"] == "fallback"
    assert json.loads(client.generate_json_text("two", system_prompt="system"))["value"] == "fallback"

    assert ["gemini-3.5" in url for url, _ in calls] == [True, False, False]
    assert "temperature" not in calls[0][1]["generationConfig"]
    assert calls[1][1]["generationConfig"]["temperature"] == 0.2
    assert any("reason=HTTP 429" in message for message in logger.messages)


def test_model_not_found_switches_without_retrying_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(monkeypatch)
    urls: list[str] = []

    def fake_post(url: str, **_: object) -> FakeResponse:
        urls.append(url)
        if "gemini-3.5-flash-lite" in url:
            return FakeResponse(404, text='{"error":{"message":"model not found"}}')
        return _success_response("fallback")

    monkeypatch.setattr(gemini_client.requests, "post", fake_post)

    client.generate_json_text("prompt", system_prompt="system")

    assert len(urls) == 2
    assert "gemini-3.5-flash-lite" in urls[0]
    assert "gemini-3.1-flash-lite" in urls[1]


def test_transient_primary_failure_falls_back_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    client, logger = _client(monkeypatch, max_retries=1)
    urls: list[str] = []

    def fake_post(url: str, **_: object) -> FakeResponse:
        urls.append(url)
        if "gemini-3.5-flash-lite" in url:
            return FakeResponse(503, text='{"error":{"status":"UNAVAILABLE"}}')
        return _success_response("fallback")

    monkeypatch.setattr(gemini_client.requests, "post", fake_post)
    monkeypatch.setattr(gemini_client.time, "sleep", lambda _: None)

    client.generate_json_text("prompt", system_prompt="system")

    assert len(urls) == 3
    assert "gemini-3.5-flash-lite" in urls[0]
    assert "gemini-3.5-flash-lite" in urls[1]
    assert "gemini-3.1-flash-lite" in urls[2]
    assert any("reason=HTTP 503" in message for message in logger.messages)


def test_fallback_quota_error_uses_normal_retry_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(monkeypatch, max_retries=1)
    urls: list[str] = []

    def fake_post(url: str, **_: object) -> FakeResponse:
        urls.append(url)
        return FakeResponse(429, text='{"error":{"status":"RESOURCE_EXHAUSTED","message":"retry in 0s"}}')

    monkeypatch.setattr(gemini_client.requests, "post", fake_post)
    monkeypatch.setattr(gemini_client.time, "sleep", lambda _: None)

    with pytest.raises(AppError, match="HTTP 429"):
        client.generate_json_text("prompt", system_prompt="system")

    assert len(urls) == 3
    assert "gemini-3.5-flash-lite" in urls[0]
    assert all("gemini-3.1-flash-lite" in url for url in urls[1:])
