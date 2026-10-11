"""Anthropic Messages API adapter.

Structured output goes through a forced tool call rather than prompt
instructions - the model then emits the object as validated tool input,
which is materially more reliable for the codegen stage.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from app.llm.base import ChatProvider, ChatRequest, Completion, ProviderError, Usage

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
EMIT_TOOL = "emit_result"


class AnthropicProvider(ChatProvider):
    name = "anthropic"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    def _payload(self, spec, request: ChatRequest, stream: bool) -> dict:
        system_blocks = []
        for block in request.system:
            if not block.text:
                continue
            entry: dict = {"type": "text", "text": block.text}
            if block.cacheable and spec.supports_prompt_cache:
                entry["cache_control"] = {"type": "ephemeral"}
            system_blocks.append(entry)

        payload: dict = {
            "model": spec.api_model,
            "max_tokens": min(request.max_tokens, spec.max_output_tokens),
            "temperature": request.temperature,
            "messages": [m.as_dict() for m in request.messages],
        }
        if system_blocks:
            payload["system"] = system_blocks
        if stream:
            payload["stream"] = True
        if request.json_schema:
            payload["tools"] = [
                {
                    "name": EMIT_TOOL,
                    "description": "Return the result as a structured object.",
                    "input_schema": request.json_schema,
                }
            ]
            payload["tool_choice"] = {"type": "tool", "name": EMIT_TOOL}
        return payload

    @staticmethod
    def _usage(raw: dict) -> Usage:
        return Usage(
            input_tokens=raw.get("input_tokens", 0),
            output_tokens=raw.get("output_tokens", 0),
            cache_write_tokens=raw.get("cache_creation_input_tokens", 0),
            cache_read_tokens=raw.get("cache_read_input_tokens", 0),
        )

    def complete(self, spec, request: ChatRequest) -> Completion:
        with httpx.Client(timeout=300) as client:
            resp = client.post(API_URL, headers=self._headers(), json=self._payload(spec, request, stream=False))
        if resp.status_code >= 400:
            raise ProviderError(f"Anthropic error {resp.status_code}: {_detail(resp)}")
        body = resp.json()

        text_parts, tool_input = [], None
        for block in body.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use" and block.get("name") == EMIT_TOOL:
                tool_input = block.get("input")

        text = json.dumps(tool_input) if tool_input is not None else "".join(text_parts)
        return Completion(text=text, usage=self._usage(body.get("usage", {})))

    async def stream(self, spec, request: ChatRequest) -> AsyncIterator[dict]:
        payload = self._payload(spec, request, stream=True)
        usage = Usage()
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", API_URL, headers=self._headers(), json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise ProviderError(f"Anthropic error {resp.status_code}: {body.decode('utf-8', 'replace')}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    event = json.loads(data)
                    etype = event.get("type")
                    if etype == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield {"type": "text", "text": delta["text"]}
                        elif delta.get("type") == "input_json_delta":
                            yield {"type": "json_delta", "text": delta.get("partial_json", "")}
                    elif etype == "message_start":
                        usage = self._usage(event.get("message", {}).get("usage", {}))
                    elif etype == "message_delta":
                        incremental = event.get("usage", {})
                        usage.output_tokens = incremental.get("output_tokens", usage.output_tokens)
                    elif etype == "error":
                        raise ProviderError(f"Anthropic stream error: {event.get('error')}")
        yield {"type": "usage", "usage": usage}


def _detail(resp: httpx.Response) -> str:
    try:
        return resp.json().get("error", {}).get("message", resp.text)
    except Exception:  # noqa: BLE001
        return resp.text
