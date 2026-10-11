"""Readiness reporting.

Every external dependency is checked independently so a failure points at
one thing: a bad PAT looks different from a missing warehouse id, which
looks different from an unreadable framework checkout.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from app.config import get_settings
from app.context.databricks_client import DatabricksError, get_client
from app.context.store import get_store

router = APIRouter(prefix="/api", tags=["health"])


def _mask(token: str | None) -> str | None:
    if not token or len(token) < 12:
        return None
    return f"{token[:6]}...{token[-4:]}"


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    client = get_client()

    databricks: dict = {
        "server_hostname": client.hostname or None,
        "warehouse_id": client.warehouse_id or None,
        "workspace_id": client.workspace_id,
        "token_present": bool(client.token),
        "token_preview": _mask(client.token),
        "status": "not_configured",
    }
    if client.token and client.hostname:
        try:
            who = client.current_user()
            databricks["status"] = "ok"
            databricks["identity"] = who.get("userName") or who.get("displayName")
        except (DatabricksError, Exception) as exc:  # noqa: BLE001 - surfaced verbatim
            databricks["status"] = "error"
            databricks["detail"] = str(exc)

    providers = {}
    for provider in ("anthropic", "openai", "google"):
        key = settings.api_key(provider)
        providers[provider] = {"configured": bool(key), "key_preview": _mask(key)}

    repo = settings.framework_repo
    framework = {
        "path": str(repo),
        "exists": repo.exists(),
        "config_loader": (repo / "src/ingestion_framework/config/config_loader.py").exists(),
        "job_template": (repo / "jobs/job_template.json").exists(),
        "projects": sorted(p.name for p in (repo / "projects").glob("*/")) if (repo / "projects").exists() else [],
    }

    store = get_store()
    snapshot = store.snapshot_summary()

    ready = (
        databricks["status"] == "ok"
        and framework["exists"]
        and any(p["configured"] for p in providers.values())
    )

    return {
        "ready": ready,
        "databricks": databricks,
        "llm_providers": providers,
        "framework": framework,
        "snapshot": snapshot,
        "embeddings_configured": bool(settings.api_key("openai")),
    }


@router.get("/config")
def client_config() -> dict:
    """Everything the frontend needs to render before any user action."""
    settings = get_settings()
    return {
        "models": [m.as_dict() for m in settings.models.values()],
        "stage_models": settings.get("stages", default={}),
        "workspace_repo_root": settings.workspace_repo_root,
        "budget": settings.section("budget"),
        "default_catalogs": settings.get("databricks", "catalogs", default=[]),
        "job_compute": settings.get("databricks", "job_compute", default="serverless"),
        "serverless_performance_target": settings.get(
            "databricks", "serverless_performance_target", default="STANDARD"
        ),
    }
