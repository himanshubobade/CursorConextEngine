"""The two calls the rest of the app makes: one structured, one streamed."""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Sequence

from app.config import ModelSpec, get_settings
from app.llm.base import ChatRequest, Message, SystemBlock, Usage, extract_json, join_user_content
from app.llm.cost import enforce_budget, record
from app.llm.registry import get_provider

log = logging.getLogger(__name__)


def resolve_model(stage: str, model_id: str | None = None) -> ModelSpec:
    return get_settings().model(model_id, stage)


def _system_blocks(system: str | Sequence[SystemBlock]) -> list[SystemBlock]:
    if isinstance(system, str):
        return [SystemBlock(system, cacheable=len(system) > 2000)]
    return list(system)


def complete_json(
    stage: str,
    system: str | Sequence[SystemBlock],
    user: Any,
    schema: dict,
    model_id: str | None = None,
    session_id: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 8000,
    history: Sequence[Message] | None = None,
) -> dict:
    """Run a structured stage and return the parsed object.

    `user` may be a string or any JSON-serializable payload; dicts are
    pretty-printed so the model sees labelled fields rather than a blob.
    """
    spec = resolve_model(stage, model_id)
    enforce_budget(session_id)

    messages = list(history or [])
    messages.append(Message(role="user", content=join_user_content([user])))

    request = ChatRequest(
        system=_system_blocks(system),
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        json_schema=schema,
    )
    provider = get_provider(spec)
    completion = provider.complete(spec, request)
    record(session_id, stage, spec, completion.usage)

    try:
        return extract_json(completion.text)
    except Exception as exc:  # noqa: BLE001
        log.error("stage %s returned unparseable JSON: %s", stage, completion.text[:500])
        raise ValueError(f"{spec.id} did not return valid JSON for stage '{stage}': {exc}") from exc


def complete_text(
    stage: str,
    system: str | Sequence[SystemBlock],
    user: Any,
    model_id: str | None = None,
    session_id: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 2000,
    history: Sequence[Message] | None = None,
) -> str:
    spec = resolve_model(stage, model_id)
    enforce_budget(session_id)
    messages = list(history or [])
    messages.append(Message(role="user", content=join_user_content([user])))
    request = ChatRequest(
        system=_system_blocks(system),
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    completion = get_provider(spec).complete(spec, request)
    record(session_id, stage, spec, completion.usage)
    return completion.text


async def stream_text(
    stage: str,
    system: str | Sequence[SystemBlock],
    messages: Sequence[Message],
    model_id: str | None = None,
    session_id: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 2000,
) -> AsyncIterator[dict]:
    """Yield `{"type": "text", ...}` chunks, then one `{"type": "usage"}`
    carrying the priced usage for this call."""
    spec = resolve_model(stage, model_id)
    enforce_budget(session_id)
    request = ChatRequest(
        system=_system_blocks(system),
        messages=list(messages),
        max_tokens=max_tokens,
        temperature=temperature,
    )
    provider = get_provider(spec)
    async for event in provider.stream(spec, request):
        if event.get("type") == "usage":
            usage: Usage = event["usage"]
            yield {"type": "usage", **record(session_id, stage, spec, usage)}
        else:
            yield event
