"""Provider-neutral chat types.

The surface is deliberately small - streamed text, one structured JSON
result, and normalized token usage - because that is all the pipeline needs.
Everything provider-specific (tool-call shapes, cache markers, SSE framing)
is absorbed by the adapters so the agent code never branches on provider.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Literal

Role = Literal["user", "assistant"]


@dataclass
class SystemBlock:
    """A chunk of system prompt. `cacheable` marks the static prefix that
    providers with explicit prompt caching should keep warm - the framework
    spec card and approved artifacts, which are identical across every turn
    of a session."""

    text: str
    cacheable: bool = False


@dataclass
class Message:
    role: Role
    content: str

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_read_tokens": self.cache_read_tokens,
        }

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class ChatRequest:
    system: list[SystemBlock] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    max_tokens: int = 4096
    temperature: float = 0.2
    json_schema: dict | None = None

    def system_text(self) -> str:
        return "\n\n".join(b.text for b in self.system if b.text)


@dataclass
class Completion:
    text: str
    usage: Usage


class ProviderError(RuntimeError):
    pass


class ChatProvider:
    """Adapter contract. `complete` is used for the structured stages,
    `stream` for anything the analyst watches arrive."""

    name: str = "base"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ProviderError(f"No API key configured for provider '{self.name}'.")
        self.api_key = api_key

    def complete(self, spec, request: ChatRequest) -> Completion:
        raise NotImplementedError

    async def stream(self, spec, request: ChatRequest) -> AsyncIterator[dict]:
        raise NotImplementedError
        yield {}  # pragma: no cover - makes this an async generator


def schema_instruction(schema: dict) -> str:
    """Appended for providers without strict schema enforcement. Explicit
    beats implicit here: models follow a printed schema plus a 'JSON only'
    instruction far more reliably than a prose description."""
    return (
        "Respond with a single JSON object and nothing else - no prose, no markdown fence. "
        "It must conform to this JSON Schema:\n"
        f"{json.dumps(schema, indent=2)}"
    )


def extract_json(text: str) -> dict:
    """Parse a model response that should be JSON, tolerating a markdown
    fence or a sentence of preamble."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```", 2)[1]
        if stripped.lstrip().startswith("json"):
            stripped = stripped.lstrip()[4:]
        stripped = stripped.rsplit("```", 1)[0]
    stripped = stripped.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start != -1 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


def join_user_content(parts: Iterable[Any]) -> str:
    out = []
    for part in parts:
        out.append(part if isinstance(part, str) else json.dumps(part, indent=2, default=str))
    return "\n\n".join(out)
