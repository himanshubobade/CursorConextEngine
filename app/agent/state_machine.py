"""The six-stage pipeline and its four approval gates.

Each stage is one prompt with a fixed output schema, and each gate freezes
an artifact into the session. Later stages read the frozen artifact instead
of replaying the conversation, which is what keeps cost flat: approving a
plan does not make the codegen call carry the whole table-discovery
transcript.

Stages and the state they leave the session in:

    intake        -> table_review   intent extracted, candidates retrieved
    table_review  -> plan_review    analyst confirmed the tables
    plan_review   -> code_review    plan approved, code generated and validated
    code_review   -> publish_ready  analyst approved the code
    publish_ready -> deployed       handled by the publish and deploy APIs
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Callable

from app.agent.artifacts import (
    CODE_SCHEMA,
    INTENT_SCHEMA,
    PLAN_SCHEMA,
    is_catalog_table,
    plan_catalog_sources,
    plan_raw_df_columns,
)
from app.agent.prompts import render_prompt
from app.agent.validators import validate_project
from app.config import get_settings
from app.context import framework_spec
from app.context.lineage_graph import neighborhood, projected_graph
from app.context.retrieval import multi_search
from app.context.store import get_store
from app.llm.base import SystemBlock
from app.llm.client import complete_json
from app.llm.cost import session_totals

log = logging.getLogger(__name__)

INTAKE = "intake"
TABLE_REVIEW = "table_review"
PLAN_REVIEW = "plan_review"
CODE_REVIEW = "code_review"
PUBLISH_READY = "publish_ready"
DEPLOYED = "deployed"

STAGE_ORDER = [INTAKE, TABLE_REVIEW, PLAN_REVIEW, CODE_REVIEW, PUBLISH_READY, DEPLOYED]


class StageError(RuntimeError):
    pass


# ----------------------------------------------------------------------
# context assembly
# ----------------------------------------------------------------------


def table_context(table_ids: list[str]) -> list[dict]:
    """Full column detail plus immediate lineage for the confirmed tables.

    This is the one place a large payload is justified: the plan has to be
    written against real column names and types, and guessing them is what
    produces code that fails validation three steps later.
    """
    store = get_store()
    out = []
    for tid in table_ids:
        if not is_catalog_table(tid):
            continue
        meta = store.get_table(tid) or {}
        up, down = store.neighbors(tid)
        out.append(
            {
                "table": tid,
                "description": meta.get("comment"),
                "table_type": meta.get("table_type"),
                "owner": meta.get("owner"),
                "columns": [
                    {
                        "name": c["column_name"],
                        "type": c["data_type"],
                        "description": c["comment"],
                    }
                    for c in store.get_columns(tid)
                ],
                "upstream": up[:8],
                "downstream": down[:8],
            }
        )
    return out


def lineage_context(table_ids: list[str]) -> dict:
    store = get_store()
    reach = neighborhood(store, table_ids, hops=1)
    return {
        "neighbours": [
            {"table": tid, "distance": info.distance, "direction": info.direction}
            for tid, info in sorted(reach.items(), key=lambda kv: kv[1].distance)
            if tid not in table_ids
        ][:40]
    }


def _spec_blocks(plan: dict | None = None) -> list[SystemBlock]:
    """System prompt for the framework-aware stages.

    The spec card is marked cacheable and comes first, so providers with
    explicit prompt caching bill it at the cached rate from the second call
    of a session onward.
    """
    source_type = (plan or {}).get("source_type") or "delta_table"
    sink_type = "table"
    write_mode = (plan or {}).get("sink_write_mode")
    card = framework_spec.spec_card(source_type=source_type, sink_type=sink_type, write_mode=write_mode)
    return [SystemBlock(card, cacheable=True)]


# ----------------------------------------------------------------------
# stages
# ----------------------------------------------------------------------


def _extract_intent(text: str, model_id: str | None, session_id: str) -> dict:
    return complete_json(
        stage="intent",
        system=render_prompt("intent"),
        user={"analyst_request": text},
        schema=INTENT_SCHEMA,
        model_id=model_id,
        session_id=session_id,
        max_tokens=1500,
    )


def _discover(intent: dict, extra: str | None, model_id: str | None, session_id: str) -> dict:
    queries = list(intent.get("search_queries") or [])
    for named in intent.get("mentioned_tables") or []:
        queries.append(named)
    if extra:
        queries.append(extra)
    queries = [q for i, q in enumerate(queries) if q and q not in queries[:i]][:6]
    return multi_search(
        queries=queries,
        intent_summary=intent.get("restated_goal") or " ".join(queries),
        model_id=model_id,
        session_id=session_id,
    )


def _build_plan(
    intent: dict,
    table_ids: list[str],
    feedback: str | None,
    previous: dict | None,
    model_id: str | None,
    session_id: str,
) -> dict:
    user: dict[str, Any] = {
        "analyst_request": intent.get("restated_goal"),
        "metrics": intent.get("metrics"),
        "filters": intent.get("filters"),
        "grain": intent.get("grain"),
        "time_range": intent.get("time_range"),
        "suggested_project_name": intent.get("project_name"),
        "confirmed_tables": table_context(table_ids),
        "surrounding_lineage": lineage_context(table_ids),
        "existing_project_names": framework_spec.existing_project_names(),
    }
    if previous and feedback:
        user["previous_plan"] = previous
        user["analyst_feedback"] = feedback
    return complete_json(
        stage="plan",
        system=_spec_blocks(previous),
        user=user,
        schema=PLAN_SCHEMA,
        model_id=model_id,
        session_id=session_id,
        max_tokens=6000,
    )


def _generate_code(
    plan: dict,
    table_ids: list[str],
    feedback: str | None,
    previous: dict | None,
    model_id: str | None,
    session_id: str,
) -> dict:
    user: dict[str, Any] = {
        "approved_plan": plan,
        "tables": table_context(table_ids),
    }
    if previous and feedback:
        user["previous_code"] = {
            "config_yaml": previous.get("config_yaml"),
            "notebook_py": previous.get("notebook_py"),
        }
        user["analyst_feedback"] = feedback

    return complete_json(
        stage="codegen",
        system=_spec_blocks(plan) + [SystemBlock(render_prompt("codegen"))],
        user=user,
        schema=CODE_SCHEMA,
        model_id=model_id,
        session_id=session_id,
        max_tokens=12000,
    )


def _repair_code(
    plan: dict,
    table_ids: list[str],
    code: dict,
    feedback: str,
    model_id: str | None,
    session_id: str,
) -> dict:
    return complete_json(
        stage="repair",
        system=_spec_blocks(plan) + [SystemBlock(render_prompt("codegen")), SystemBlock(render_prompt("repair"))],
        user={
            "approved_plan": plan,
            "tables": table_context(table_ids),
            "generated_config_yaml": code.get("config_yaml"),
            "generated_notebook_py": code.get("notebook_py"),
            "validation_failures": feedback,
        },
        schema=CODE_SCHEMA,
        model_id=model_id,
        session_id=session_id,
        max_tokens=12000,
    )


def generate_validated_code(
    plan: dict,
    table_ids: list[str],
    feedback: str | None,
    previous: dict | None,
    model_id: str | None,
    session_id: str,
    on_attempt: Callable[[int, dict], None] | None = None,
) -> dict:
    """Generate, validate, and repair until clean or out of attempts.

    The analyst only ever sees output that has already been through the
    framework's own CI validators, so code review is about business logic
    rather than syntax.
    """
    max_attempts = int(get_settings().get("codegen", "max_repair_attempts", default=2))
    project_name = plan.get("project_name") or "generated_project"
    sources = plan_catalog_sources(plan) or [t for t in table_ids if is_catalog_table(t)]
    raw_columns = plan_raw_df_columns(plan)

    code = _generate_code(plan, table_ids, feedback, previous, model_id, session_id)
    attempts = [{"attempt": 1, "repaired": False}]

    for attempt in range(1, max_attempts + 2):
        result = validate_project(
            project_name=project_name,
            config_yaml=code.get("config_yaml", ""),
            notebook_py=code.get("notebook_py", ""),
            source_tables=sources,
            raw_df_columns=raw_columns,
        )
        attempts[-1]["validation"] = result.as_dict()
        if on_attempt:
            on_attempt(attempt, result.as_dict())
        if result.ok or attempt > max_attempts:
            code["validation"] = result.as_dict()
            code["attempts"] = attempts
            code["project_name"] = project_name
            return code

        log.info("codegen attempt %s failed validation, repairing", attempt)
        code = _repair_code(plan, table_ids, code, result.feedback(), model_id, session_id)
        attempts.append({"attempt": attempt + 1, "repaired": True})

    return code


# ----------------------------------------------------------------------
# narration (deterministic, so it costs nothing and cannot hallucinate)
# ----------------------------------------------------------------------


def _describe_candidates(discovery: dict) -> str:
    results = discovery.get("results") or []
    if not results:
        return (
            "I could not find any matching tables. "
            + (discovery.get("warning") or "Try naming a table or a column you know exists.")
        )
    lines = [f"Found {len(results)} candidate tables for this request. In order of relevance:", ""]
    for card in results:
        reason = card.get("reason") or "matched on " + ", ".join(card.get("matched_columns", [])[:3])
        lines.append(f"- **{card['table_id']}** - {reason}")
    lines.append("")
    lines.append("Confirm the ones this pipeline should read, or tell me what is missing.")
    return "\n".join(lines)


def _describe_plan(plan: dict) -> str:
    lines = [plan.get("summary", ""), ""]
    lines.append(f"**Reads** {plan.get('primary_source_table')}" + (
        " plus " + ", ".join(plan.get("additional_source_tables") or []) if plan.get("additional_source_tables") else ""
    ))
    lines.append(f"**Writes** {plan.get('sink_table')} using `{plan.get('sink_write_mode')}`"
                 + (f" on {', '.join(plan.get('merge_keys') or [])}" if plan.get("merge_keys") else ""))
    lines.append(f"**Produces** {len(plan.get('sink_columns') or [])} columns")
    if plan.get("assumptions"):
        lines += ["", "I assumed:"] + [f"- {a}" for a in plan["assumptions"]]
    if plan.get("open_questions"):
        lines += ["", "I could not decide these:"] + [f"- {q}" for q in plan["open_questions"]]
    return "\n".join(lines)


def _describe_code(code: dict) -> str:
    validation = code.get("validation") or {}
    if validation.get("ok"):
        summary = "Both files passed every validator: the framework's config schema check, its "\
                  "transform contract check, a syntax and undefined-name pass, and a column "\
                  "existence check against the catalog snapshot."
    else:
        failures = validation.get("errors") or []
        summary = (
            f"The generated code still fails {len(failures)} check(s) after "
            f"{len(code.get('attempts') or [])} attempt(s):\n"
            + "\n".join(f"- [{f['check']}] {f['message']}" for f in failures[:6])
        )
    warnings = validation.get("warnings") or []
    if warnings:
        summary += "\n\nWarnings:\n" + "\n".join(f"- {w['message']}" for w in warnings[:4])
    if code.get("notes"):
        summary += f"\n\n{code['notes']}"
    return summary


# ----------------------------------------------------------------------
# driver
# ----------------------------------------------------------------------


async def run(session_id: str, action: str, payload: dict) -> AsyncIterator[dict]:
    """Advance the session by one action, yielding UI events as it goes."""
    store = get_store()
    session = store.get_session(session_id)
    if session is None:
        yield {"type": "error", "message": f"Unknown session {session_id}"}
        return

    state = session["state"]
    stage = session["stage"]
    model_id = payload.get("model_id") or state.get("model_id")
    if model_id:
        state["model_id"] = model_id
    text = (payload.get("text") or "").strip()

    try:
        if action == "message":
            if stage == INTAKE:
                async for event in _do_intake(session_id, state, text, model_id):
                    yield event
            elif stage == TABLE_REVIEW:
                async for event in _do_discovery(session_id, state, text, model_id):
                    yield event
            elif stage == PLAN_REVIEW:
                async for event in _do_plan(session_id, state, text, model_id):
                    yield event
            elif stage in (CODE_REVIEW, PUBLISH_READY, DEPLOYED):
                async for event in _do_codegen(session_id, state, text, model_id):
                    yield event
            else:
                yield {"type": "error", "message": f"Nothing to do in stage {stage}"}

        elif action == "approve_tables":
            selected = payload.get("table_ids") or []
            if not selected:
                yield {"type": "error", "message": "Select at least one table before continuing."}
                return
            state["selected_tables"] = selected
            store.save_artifact(session_id, "tables", {"selected": selected}, approved=True)
            async for event in _do_plan(session_id, state, None, model_id):
                yield event

        elif action == "approve_plan":
            plan = state.get("plan")
            if not plan:
                yield {"type": "error", "message": "There is no plan to approve yet."}
                return
            artifact = store.latest_artifact(session_id, "plan")
            if artifact:
                store.approve_artifact(artifact["id"])
            async for event in _do_codegen(session_id, state, None, model_id):
                yield event

        elif action == "approve_code":
            code = state.get("code")
            if not code:
                yield {"type": "error", "message": "There is no code to approve yet."}
                return
            artifact = store.latest_artifact(session_id, "code")
            if artifact:
                store.approve_artifact(artifact["id"])
            _advance(session_id, state, PUBLISH_READY)
            yield {"type": "stage", "stage": PUBLISH_READY}
            yield _assistant(
                session_id,
                PUBLISH_READY,
                "Code approved. Publish it to the framework repository and the Databricks "
                "workspace, then set a schedule.",
            )

        elif action == "reset_to":
            target = payload.get("stage")
            if target not in STAGE_ORDER:
                yield {"type": "error", "message": f"Unknown stage {target}"}
                return
            _advance(session_id, state, target)
            yield {"type": "stage", "stage": target}

        else:
            yield {"type": "error", "message": f"Unknown action {action}"}

    except Exception as exc:  # noqa: BLE001 - surfaced to the analyst as a chat error
        log.exception("stage failed")
        yield {"type": "error", "message": str(exc)}
        return

    yield {"type": "usage_total", **session_totals(session_id)}
    yield {"type": "done", "stage": store.get_session(session_id)["stage"]}


# ----------------------------------------------------------------------
# stage runners
# ----------------------------------------------------------------------


def _advance(session_id: str, state: dict, stage: str) -> None:
    get_store().update_session(session_id, stage=stage, state=state)


def _assistant(session_id: str, stage: str, content: str, meta: dict | None = None) -> dict:
    get_store().add_message(session_id, "assistant", content, stage=stage, meta=meta)
    return {"type": "message", "role": "assistant", "content": content, "stage": stage}


async def _do_intake(session_id: str, state: dict, text: str, model_id: str | None) -> AsyncIterator[dict]:
    store = get_store()
    if not text:
        yield {"type": "error", "message": "Describe what you need before sending."}
        return

    yield {"type": "status", "message": "Reading the request"}
    intent = await asyncio.to_thread(_extract_intent, text, model_id, session_id)
    state["intent"] = intent
    store.save_artifact(session_id, "intent", intent)
    store.update_session(session_id, title=intent.get("restated_goal", "")[:120], state=state)
    yield {"type": "artifact", "kind": "intent", "payload": intent}

    if intent.get("clarifications"):
        message = "Before I search, I need to know:\n" + "\n".join(f"- {c}" for c in intent["clarifications"])
        yield _assistant(session_id, INTAKE, message)
        return

    async for event in _do_discovery(session_id, state, None, model_id):
        yield event


async def _do_discovery(session_id: str, state: dict, extra: str | None, model_id: str | None) -> AsyncIterator[dict]:
    store = get_store()
    intent = state.get("intent")
    if not intent:
        yield {"type": "error", "message": "No request captured yet."}
        return

    yield {"type": "status", "message": "Searching the catalog"}
    discovery = await asyncio.to_thread(_discover, intent, extra, model_id, session_id)
    state["candidates"] = discovery.get("results", [])
    state["discovery"] = {k: v for k, v in discovery.items() if k != "all_candidates"}
    state.setdefault("selected_tables", [c["table_id"] for c in discovery.get("results", [])[:3]])
    store.save_artifact(session_id, "candidates", discovery)

    _advance(session_id, state, TABLE_REVIEW)
    yield {"type": "stage", "stage": TABLE_REVIEW}
    yield {"type": "artifact", "kind": "candidates", "payload": discovery}
    yield _assistant(session_id, TABLE_REVIEW, _describe_candidates(discovery))


async def _do_plan(session_id: str, state: dict, feedback: str | None, model_id: str | None) -> AsyncIterator[dict]:
    store = get_store()
    tables = state.get("selected_tables") or []
    if not tables:
        yield {"type": "error", "message": "No tables confirmed yet."}
        return

    yield {"type": "status", "message": "Designing the pipeline"}
    plan = await asyncio.to_thread(
        _build_plan, state.get("intent", {}), tables, feedback, state.get("plan"), model_id, session_id
    )
    state["plan"] = plan
    store.save_artifact(session_id, "plan", plan)

    yield {"type": "status", "message": "Projecting lineage"}
    graph = await asyncio.to_thread(
        projected_graph,
        None,
        plan_catalog_sources(plan) or [t for t in tables if is_catalog_table(t)],
        plan.get("sink_table"),
        plan.get("project_name", "project"),
    )
    state["projected_lineage"] = graph

    _advance(session_id, state, PLAN_REVIEW)
    yield {"type": "stage", "stage": PLAN_REVIEW}
    yield {"type": "artifact", "kind": "plan", "payload": plan}
    yield {"type": "artifact", "kind": "projected_lineage", "payload": graph}
    yield _assistant(session_id, PLAN_REVIEW, _describe_plan(plan))


async def _do_codegen(session_id: str, state: dict, feedback: str | None, model_id: str | None) -> AsyncIterator[dict]:
    store = get_store()
    plan = state.get("plan")
    if not plan:
        yield {"type": "error", "message": "Approve a plan first."}
        return

    yield {"type": "status", "message": "Writing the project files"}

    attempt_events: list[dict] = []

    def on_attempt(attempt: int, validation: dict) -> None:
        attempt_events.append({"attempt": attempt, "ok": validation["ok"], "errors": len(validation["errors"])})

    code = await asyncio.to_thread(
        generate_validated_code,
        plan,
        state.get("selected_tables") or [],
        feedback,
        state.get("code"),
        model_id,
        session_id,
        on_attempt,
    )
    for event in attempt_events:
        yield {
            "type": "status",
            "message": f"Validation attempt {event['attempt']}: "
            + ("passed" if event["ok"] else f"{event['errors']} problem(s), repairing"),
        }

    state["code"] = code
    store.save_artifact(session_id, "code", code)

    _advance(session_id, state, CODE_REVIEW)
    yield {"type": "stage", "stage": CODE_REVIEW}
    yield {"type": "artifact", "kind": "code", "payload": code}
    yield _assistant(session_id, CODE_REVIEW, _describe_code(code))
