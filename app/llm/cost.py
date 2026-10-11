"""Token and spend accounting.

Every call is priced from models.yaml and written to usage_log, so the live
meter in the UI and the per-session hard cap read from the same source.
"""
from __future__ import annotations

from app.config import ModelSpec, get_settings
from app.context.store import get_store
from app.llm.base import Usage


class BudgetExceeded(RuntimeError):
    pass


def record(session_id: str | None, stage: str, spec: ModelSpec, usage: Usage) -> dict:
    cost = spec.cost(usage.as_dict())
    get_store().log_usage(session_id, stage, spec.id, usage.as_dict(), cost)
    return {"model": spec.id, "stage": stage, "usage": usage.as_dict(), "cost_usd": round(cost, 6)}


def session_totals(session_id: str) -> dict:
    totals = get_store().session_usage(session_id)
    budget = get_settings().section("budget")
    max_usd = float(budget.get("max_usd_per_session", 0) or 0)
    max_tokens = int(budget.get("max_tokens_per_session", 0) or 0)
    totals["budget_usd"] = max_usd
    totals["budget_tokens"] = max_tokens
    totals["fraction_used"] = round(totals["cost_usd"] / max_usd, 3) if max_usd else 0.0
    return totals


def enforce_budget(session_id: str | None) -> None:
    """Called before each billable call. Checking beforehand means a session
    can overshoot by at most one call rather than running unbounded."""
    if not session_id:
        return
    budget = get_settings().section("budget")
    max_usd = float(budget.get("max_usd_per_session", 0) or 0)
    max_tokens = int(budget.get("max_tokens_per_session", 0) or 0)
    if not max_usd and not max_tokens:
        return

    totals = get_store().session_usage(session_id)
    if max_usd and totals["cost_usd"] >= max_usd:
        raise BudgetExceeded(
            f"Session budget of ${max_usd:.2f} reached (${totals['cost_usd']:.2f} spent). "
            "Raise budget.max_usd_per_session in settings.yaml or start a new session."
        )
    if max_tokens and totals["total_tokens"] >= max_tokens:
        raise BudgetExceeded(
            f"Session token cap of {max_tokens:,} reached ({totals['total_tokens']:,} used)."
        )
