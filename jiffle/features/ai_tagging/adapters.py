import base64
import json
import re

import requests

from jiffle.configuration.settings import Settings
from jiffle.features.ai_tagging.contracts import MediaSample, TaggingResult


class TaggingProviderFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class OpenAICompatibleTaggingProvider:
    provider_name = "openai-compatible"

    def __init__(self, url: str, model: str, prompt: str, api_key: str | None):
        self.url = url
        self.model = model
        self.prompt = prompt
        self.api_key = api_key

    def suggest_tags(self, samples: tuple[MediaSample, ...]) -> TaggingResult:
        content = [{"type": "text", "text": self.prompt}]
        content.extend({
            "type": "image_url",
            "image_url": {"url": _data_url(sample)},
        } for sample in samples)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = requests.post(
                self.url,
                headers=headers,
                json={"model": self.model, "messages": [{"role": "user", "content": content}]},
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            text = payload["choices"][0]["message"]["content"]
            tags = _parse_tags(text)
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as error:
            raise TaggingProviderFailure(
                "ai.provider_error", "The tagging provider returned an invalid response."
            ) from error
        return TaggingResult(tags, {"response_id": payload.get("id")})

    def check_connection(self) -> None:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        models_url = self.url.rsplit("/chat/completions", 1)[0] + "/models"
        response = requests.get(models_url, headers=headers, timeout=15)
        response.raise_for_status()


class GeminiTaggingProvider:
    provider_name = "gemini"

    def __init__(self, url: str, model: str, prompt: str, api_key: str):
        self.url = url.rstrip("/")
        self.model = model
        self.prompt = prompt
        self.api_key = api_key

    def suggest_tags(self, samples: tuple[MediaSample, ...]) -> TaggingResult:
        parts = [{"text": self.prompt}]
        parts.extend({"inline_data": {
            "mime_type": sample.mime_type,
            "data": base64.b64encode(sample.content).decode("ascii"),
        }} for sample in samples)
        endpoint = f"{self.url}/models/{self.model}:generateContent"
        try:
            response = requests.post(
                endpoint, params={"key": self.api_key},
                json={"contents": [{"parts": parts}]}, timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
            tags = _parse_tags(text)
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as error:
            raise TaggingProviderFailure(
                "ai.provider_error", "The tagging provider returned an invalid response."
            ) from error
        return TaggingResult(tags, {"candidates": len(payload.get("candidates", []))})

    def check_connection(self) -> None:
        endpoint = f"{self.url}/models/{self.model}"
        response = requests.get(endpoint, params={"key": self.api_key}, timeout=15)
        response.raise_for_status()


def build_tagging_provider(settings: Settings):
    if not settings.ai_api_url or not settings.ai_api_model:
        return None
    if settings.ai_api_format == "gemini":
        if not settings.ai_api_key:
            return None
        return GeminiTaggingProvider(
            settings.ai_api_url, settings.ai_api_model,
            settings.ai_tagging_prompt, settings.ai_api_key,
        )
    return OpenAICompatibleTaggingProvider(
        settings.ai_api_url, settings.ai_api_model,
        settings.ai_tagging_prompt, settings.ai_api_key,
    )


def _data_url(sample: MediaSample) -> str:
    encoded = base64.b64encode(sample.content).decode("ascii")
    return f"data:{sample.mime_type};base64,{encoded}"


def _parse_tags(raw_text: str) -> tuple[str, ...]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip())
    payload = json.loads(cleaned)
    raw_tags = payload.get("tags") if isinstance(payload, dict) else None
    if not isinstance(raw_tags, list):
        raise ValueError("tags array missing")
    normalized = {
        str(tag).strip().lower().replace(" ", "_")
        for tag in raw_tags
        if str(tag).strip()
    }
    return tuple(sorted(normalized))
