"""OpenAI Chat Completions adapter.

Prompt caching is automatic and prefix-based, so the only thing this adapter
has to do for it is keep the cacheable system blocks first and unchanged
across turns - which is how ChatRequest is assembled.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from app.llm.base import ChatProvider, ChatRequest, Completion, ProviderError, Usage, schema_instruction

API_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider(ChatProvider):
    name = "openai"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _payload(self, spec, request: ChatRequest, stream: bool) -> dict:
        messages = []
        system_text = request.system_text()
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.extend(m.as_dict() for m in request.messages)

        if request.json_schema and messages:
            messages[-1] = {
                **messages[-1],
                "content": messages[-1]["content"] + "\n\n" + schema_instruction(request.json_schema),
            }

        payload: dict = {
            "model": spec.api_model,
            "messages": messages,
            "max_tokens": min(request.max_tokens, spec.max_output_tokens),
            "temperature": request.temperature,
        }
        if request.json_schema:
            payload["response_format"] = {"type": "json_object"}
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        return payload

    @staticmethod
    def _usage(raw: dict) -> Usage:
        cached = (raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return Usage(
            input_tokens=max(0, raw.get("prompt_tokens", 0) - cached),
            output_tokens=raw.get("completion_tokens", 0),
            cache_read_tokens=cached,
        )

    def complete(self, spec, request: ChatRequest) -> Completion:
        with httpx.Client(timeout=300) as client:
            resp = client.post(API_URL, headers=self._headers(), json=self._payload(spec, request, stream=False))
        if resp.status_code >= 400:
            raise ProviderError(f"OpenAI error {resp.status_code}: {_detail(resp)}")
        body = resp.json()
        text = body["choices"][0]["message"].get("content") or ""
        return Completion(text=text, usage=self._usage(body.get("usage", {})))

    async def stream(self, spec, request: ChatRequest) -> AsyncIterator[dict]:
        payload = self._payload(spec, request, stream=True)
        usage = Usage()
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", API_URL, headers=self._headers(), json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise ProviderError(f"OpenAI error {resp.status_code}: {body.decode('utf-8', 'replace')}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    event = json.loads(data)
                    if event.get("usage"):
                        usage = self._usage(event["usage"])
                    for choice in event.get("choices", []):
                        piece = (choice.get("delta") or {}).get("content")
                        if piece:
                            yield {"type": "text", "text": piece}
        yield {"type": "usage", "usage": usage}


def _detail(resp: httpx.Response) -> str:
    try:
        return resp.json().get("error", {}).get("message", resp.text)
    except Exception:  # noqa: BLE001
        return resp.text
