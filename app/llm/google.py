"""Google Gemini adapter (generateContent / streamGenerateContent).

Gemini takes a JSON Schema subset directly as `responseSchema`, so
structured stages are enforced server-side here too - but the subset rejects
several keywords, hence the sanitizer below.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from app.llm.base import ChatProvider, ChatRequest, Completion, ProviderError, Usage

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Keywords Gemini's responseSchema dialect does not accept.
_UNSUPPORTED = {"additionalProperties", "$schema", "definitions", "$defs", "default", "examples", "title"}


def sanitize_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: sanitize_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED}
    if isinstance(schema, list):
        return [sanitize_schema(v) for v in schema]
    return schema


class GoogleProvider(ChatProvider):
    name = "google"

    def _url(self, spec, method: str) -> str:
        suffix = "?alt=sse&key=" if method == "streamGenerateContent" else "?key="
        return f"{BASE_URL}/{spec.api_model}:{method}{suffix}{self.api_key}"

    def _payload(self, spec, request: ChatRequest) -> dict:
        contents = [
            {"role": "user" if m.role == "user" else "model", "parts": [{"text": m.content}]}
            for m in request.messages
        ]
        generation: dict = {
            "temperature": request.temperature,
            "maxOutputTokens": min(request.max_tokens, spec.max_output_tokens),
        }
        if request.json_schema:
            generation["responseMimeType"] = "application/json"
            generation["responseSchema"] = sanitize_schema(request.json_schema)

        payload: dict = {"contents": contents, "generationConfig": generation}
        system_text = request.system_text()
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        return payload

    @staticmethod
    def _usage(raw: dict) -> Usage:
        cached = raw.get("cachedContentTokenCount", 0)
        return Usage(
            input_tokens=max(0, raw.get("promptTokenCount", 0) - cached),
            output_tokens=raw.get("candidatesTokenCount", 0),
            cache_read_tokens=cached,
        )

    @staticmethod
    def _text_of(candidate: dict) -> str:
        return "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []))

    def complete(self, spec, request: ChatRequest) -> Completion:
        with httpx.Client(timeout=300) as client:
            resp = client.post(
                self._url(spec, "generateContent"),
                headers={"Content-Type": "application/json"},
                json=self._payload(spec, request),
            )
        if resp.status_code >= 400:
            raise ProviderError(f"Gemini error {resp.status_code}: {_detail(resp)}")
        body = resp.json()
        candidates = body.get("candidates", [])
        text = self._text_of(candidates[0]) if candidates else ""
        return Completion(text=text, usage=self._usage(body.get("usageMetadata", {})))

    async def stream(self, spec, request: ChatRequest) -> AsyncIterator[dict]:
        usage = Usage()
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream(
                "POST",
                self._url(spec, "streamGenerateContent"),
                headers={"Content-Type": "application/json"},
                json=self._payload(spec, request),
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise ProviderError(f"Gemini error {resp.status_code}: {body.decode('utf-8', 'replace')}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    event = json.loads(data)
                    if event.get("usageMetadata"):
                        usage = self._usage(event["usageMetadata"])
                    for candidate in event.get("candidates", []):
                        text = self._text_of(candidate)
                        if text:
                            yield {"type": "text", "text": text}
        yield {"type": "usage", "usage": usage}


def _detail(resp: httpx.Response) -> str:
    try:
        return resp.json().get("error", {}).get("message", resp.text)
    except Exception:  # noqa: BLE001
        return resp.text
