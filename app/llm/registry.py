"""Provider lookup."""
from __future__ import annotations

from functools import lru_cache

from app.config import ModelSpec, get_settings
from app.llm.anthropic import AnthropicProvider
from app.llm.base import ChatProvider, ProviderError
from app.llm.google import GoogleProvider
from app.llm.openai import OpenAIProvider

PROVIDERS: dict[str, type[ChatProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "google": GoogleProvider,
}


@lru_cache(maxsize=8)
def _build(provider_name: str, api_key: str) -> ChatProvider:
    cls = PROVIDERS.get(provider_name)
    if cls is None:
        raise ProviderError(f"Unknown provider '{provider_name}'. Known: {', '.join(PROVIDERS)}")
    return cls(api_key)


def get_provider(spec: ModelSpec) -> ChatProvider:
    api_key = get_settings().api_key(spec.provider)
    if not api_key:
        raise ProviderError(
            f"Model '{spec.id}' needs a {spec.provider} API key. Add "
            f"llm.{spec.provider}_api_key to secrets.yaml or export "
            f"{spec.provider.upper()}_API_KEY."
        )
    return _build(spec.provider, api_key)


def available_models() -> list[dict]:
    """Registry entries annotated with whether their key is actually set,
    so the UI can grey out models the user cannot call."""
    settings = get_settings()
    out = []
    for spec in settings.models.values():
        entry = spec.as_dict()
        entry["available"] = bool(settings.api_key(spec.provider))
        out.append(entry)
    return out
