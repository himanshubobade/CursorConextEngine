"""Configuration: settings.yaml + models.yaml + secrets.

Secrets resolve environment-first so the app can run from a shell with
exported credentials and no secrets.yaml on disk at all.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

# Verbatim values from secrets.example.yaml, treated as "not configured".
_PLACEHOLDER_SECRETS = {
    "adb-1234567890123456.7.azuredatabricks.net",
    "/sql/1.0/warehouses/abcdef1234567890",
    "dapiXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    "sk-ant-...",
    "sk-...",
    "AIza...",
}

_ENV_KEYS = {
    ("databricks", "server_hostname"): "DATABRICKS_SERVER_HOSTNAME",
    ("databricks", "http_path"): "DATABRICKS_HTTP_PATH",
    ("databricks", "token"): "DATABRICKS_TOKEN",
    ("llm", "anthropic_api_key"): "ANTHROPIC_API_KEY",
    ("llm", "openai_api_key"): "OPENAI_API_KEY",
    ("llm", "google_api_key"): "GOOGLE_API_KEY",
}


class ModelSpec:
    """One entry from models.yaml, with the pricing arithmetic attached."""

    def __init__(self, raw: dict[str, Any]):
        self.id: str = raw["id"]
        self.provider: str = raw["provider"]
        self.api_model: str = raw.get("api_model", raw["id"])
        self.label: str = raw.get("label", raw["id"])
        self.context_window: int = raw.get("context_window", 128000)
        self.max_output_tokens: int = raw.get("max_output_tokens", 8192)
        self.supports_tools: bool = raw.get("supports_tools", True)
        self.supports_prompt_cache: bool = raw.get("supports_prompt_cache", False)
        self.price_input: float = float(raw.get("input", 0.0))
        self.price_output: float = float(raw.get("output", 0.0))
        self.price_cache_write: float = float(raw.get("cache_write", raw.get("input", 0.0)))
        self.price_cache_read: float = float(raw.get("cache_read", raw.get("input", 0.0)))
        self.dimensions: int = int(raw.get("dimensions", 0))

    def cost(self, usage: dict[str, int]) -> float:
        """USD for one call, given a normalized usage dict."""
        m = 1_000_000
        return (
            usage.get("input_tokens", 0) * self.price_input
            + usage.get("output_tokens", 0) * self.price_output
            + usage.get("cache_write_tokens", 0) * self.price_cache_write
            + usage.get("cache_read_tokens", 0) * self.price_cache_read
        ) / m

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "label": self.label,
            "context_window": self.context_window,
            "supports_tools": self.supports_tools,
            "supports_prompt_cache": self.supports_prompt_cache,
            "price_input": self.price_input,
            "price_output": self.price_output,
        }


class Settings:
    def __init__(self) -> None:
        self.root = ROOT
        self.raw: dict[str, Any] = yaml.safe_load((ROOT / "settings.yaml").read_text()) or {}
        registry = yaml.safe_load((ROOT / "models.yaml").read_text()) or {}
        self.models: dict[str, ModelSpec] = {
            m["id"]: ModelSpec(m) for m in registry.get("models", [])
        }
        self.embedding_models: dict[str, ModelSpec] = {
            m["id"]: ModelSpec(m) for m in registry.get("embeddings", [])
        }
        self._secrets = self._load_secrets()

    # ---------------- secrets ----------------

    def _load_secrets(self) -> dict[str, Any]:
        path = ROOT / "secrets.yaml"
        data: dict[str, Any] = {}
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
        for (section, key), env_name in _ENV_KEYS.items():
            value = os.environ.get(env_name)
            if value:
                data.setdefault(section, {})[key] = value
        return data

    def secret(self, section: str, key: str) -> str | None:
        value = (self._secrets.get(section) or {}).get(key)
        if value in ("", None):
            return None
        # An unedited copy of secrets.example.yaml should read as "missing"
        # rather than producing a confusing 401 from the provider.
        if str(value) in _PLACEHOLDER_SECRETS:
            return None
        return str(value)

    def api_key(self, provider: str) -> str | None:
        return self.secret("llm", f"{provider}_api_key")

    # ---------------- settings sections ----------------

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name) or {}

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in path:
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path_setting(self, key: str) -> Path:
        """Resolve a configured path relative to the ContextEngine root."""
        raw = self.get("paths", key, default="")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = (ROOT / candidate).resolve()
        return candidate

    @property
    def db_path(self) -> Path:
        path = self.path_setting("database")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def framework_repo(self) -> Path:
        return self.path_setting("framework_repo")

    @property
    def framework_clone(self) -> Path:
        return self.path_setting("framework_clone")

    @property
    def workspace_repo_root(self) -> str:
        return self.get("databricks", "workspace_repo_root", default="/Workspace/Shared")

    def stage_model(self, stage: str) -> ModelSpec:
        """Model assigned to a pipeline stage, falling back to any registered
        model so a typo in settings.yaml degrades instead of crashing."""
        model_id = self.get("stages", stage)
        if model_id and model_id in self.models:
            return self.models[model_id]
        if model_id:
            raise KeyError(
                f"stages.{stage} = '{model_id}' is not defined in models.yaml "
                f"(known: {', '.join(sorted(self.models))})"
            )
        return next(iter(self.models.values()))

    def model(self, model_id: str | None, stage: str) -> ModelSpec:
        """An explicit per-request model override, else the stage default."""
        if model_id and model_id in self.models:
            return self.models[model_id]
        return self.stage_model(stage)

    @property
    def embedding_model(self) -> ModelSpec:
        model_id = self.get("retrieval", "embedding_model", default="text-embedding-3-small")
        if model_id not in self.embedding_models:
            raise KeyError(f"retrieval.embedding_model '{model_id}' is not in models.yaml embeddings")
        return self.embedding_models[model_id]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
