from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Protocol

import requests

from app.utils import AppError, RunLogger


class LLMClient(Protocol):
    def generate_json_text(self, prompt: str, *, system_prompt: str) -> str:
        ...


class _GeminiRequestFailure(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, model_unavailable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.model_unavailable = model_unavailable


@dataclass
class GeminiClient:
    model: str
    fallback_model: str
    api_key_env: str
    temperature: float
    max_retries: int
    retry_backoff_seconds: float
    request_timeout_seconds: float
    request_interval_seconds: float
    logger: RunLogger

    def __post_init__(self) -> None:
        self.api_key = os.environ.get(self.api_key_env, "").strip()
        if not self.api_key:
            raise AppError(
                f"未检测到 Gemini API key 环境变量：{self.api_key_env}\n"
                f"请先设置环境变量，例如 PowerShell：$env:{self.api_key_env}='你的密钥'\n"
                "不要把 API key 写进代码或 config.yaml。"
            )
        self.model = self.model.strip()
        self.fallback_model = self.fallback_model.strip()
        self._active_model = self.model

    def generate_json_text(self, prompt: str, *, system_prompt: str) -> str:
        models = [self._active_model]
        if (
            self._active_model == self.model
            and self.fallback_model
            and self.fallback_model != self.model
        ):
            models.append(self.fallback_model)

        last_failure: _GeminiRequestFailure | None = None
        for index, model in enumerate(models):
            can_fallback = index + 1 < len(models)
            try:
                return self._generate_with_model(
                    model,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    can_fallback=can_fallback,
                )
            except _GeminiRequestFailure as exc:
                last_failure = exc
                if can_fallback and _should_switch_model(exc):
                    fallback = models[index + 1]
                    self._active_model = fallback
                    self.logger.write(
                        f"Gemini model fallback primary={model} fallback={fallback} "
                        f"reason={_fallback_reason(exc)}"
                    )
                    continue
                break

        message = str(last_failure) if last_failure is not None else "未知错误"
        raise AppError(f"Gemini 请求最终失败：{message}")

    def _generate_with_model(
        self,
        model: str,
        *,
        prompt: str,
        system_prompt: str,
        can_fallback: bool,
    ) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        params = {"key": self.api_key}
        generation_config: dict[str, object] = {
            "responseMimeType": "application/json",
        }
        if _model_supports_temperature(model):
            generation_config["temperature"] = self.temperature
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        last_error = ""
        last_status_code: int | None = None
        model_unavailable = False
        for attempt in range(self.max_retries + 1):
            try:
                self.logger.write(f"Gemini request start model={model} attempt={attempt + 1}")
                response = requests.post(url, params=params, json=payload, timeout=self.request_timeout_seconds)
                if response.status_code >= 400:
                    last_status_code = response.status_code
                    last_error = self._redact(f"HTTP {response.status_code}: {response.text[:1000]}")
                    model_unavailable = _is_model_unavailable(response.status_code, response.text)
                    if model_unavailable or (can_fallback and response.status_code == 429):
                        raise _GeminiRequestFailure(
                            last_error,
                            status_code=response.status_code,
                            model_unavailable=model_unavailable,
                        )
                    raise RuntimeError(last_error)
                data = response.json()
                text = _extract_text(data)
                if not text:
                    last_error = "Gemini 返回为空。"
                    raise RuntimeError(last_error)
                if self.request_interval_seconds > 0:
                    time.sleep(self.request_interval_seconds)
                return text
            except _GeminiRequestFailure:
                raise
            except Exception as exc:
                last_error = self._redact(str(exc))
                self.logger.write(f"Gemini 请求失败 model={model} attempt={attempt + 1}: {last_error}")
                if attempt < self.max_retries:
                    delay = _retry_delay_seconds(last_error, self.retry_backoff_seconds, attempt)
                    self.logger.write(f"Gemini retry sleep seconds={delay:.1f}")
                    time.sleep(delay)
        raise _GeminiRequestFailure(
            last_error,
            status_code=last_status_code,
            model_unavailable=model_unavailable,
        )

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "[REDACTED_API_KEY]") if self.api_key else text


class LocalLLMClient:
    def generate_json_text(self, prompt: str, *, system_prompt: str) -> str:
        raise AppError("local provider 尚未实现。当前仅预留接口，请使用 --provider gemini。")


def _extract_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    texts = [part.get("text", "") for part in parts if isinstance(part, dict)]
    return "\n".join(texts).strip()


def strip_json_fence(text: str) -> str:
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return stripped


def parse_json_text(text: str) -> dict:
    cleaned = strip_json_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def _retry_delay_seconds(error: str, base_delay: float, attempt: int) -> float:
    retry_match = re.search(r"retry in ([0-9.]+)s", error, flags=re.IGNORECASE)
    if retry_match:
        delay = float(retry_match.group(1)) + 3.0
        if "RESOURCE_EXHAUSTED" in error or "HTTP 429" in error:
            delay = max(delay, 20.0)
        return min(300.0, delay)
    if "RESOURCE_EXHAUSTED" in error or "HTTP 429" in error:
        return min(300.0, max(base_delay, 20.0) * (attempt + 1))
    if "UNAVAILABLE" in error or "HTTP 503" in error:
        return min(120.0, max(base_delay, 20.0) * (attempt + 1))
    return base_delay * (attempt + 1)


def _model_supports_temperature(model: str) -> bool:
    match = re.match(r"^gemini-(\d+)\.(\d+)", model.strip(), flags=re.IGNORECASE)
    if not match:
        return True
    version = (int(match.group(1)), int(match.group(2)))
    return version < (3, 5)


def _is_model_unavailable(status_code: int, response_text: str) -> bool:
    if status_code == 404:
        return True
    if status_code != 400:
        return False
    lowered = response_text.lower()
    return "model" in lowered and any(
        marker in lowered
        for marker in ("not found", "not supported", "unsupported", "not available")
    )


def _should_switch_model(failure: _GeminiRequestFailure) -> bool:
    return (
        failure.model_unavailable
        or failure.status_code == 429
        or failure.status_code in {500, 502, 503, 504}
    )


def _fallback_reason(failure: _GeminiRequestFailure) -> str:
    if failure.status_code is not None:
        return f"HTTP {failure.status_code}"
    return "model unavailable"


