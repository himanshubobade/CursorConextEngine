"""Session lifecycle and the single streaming endpoint that advances it."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent import state_machine
from app.context.store import get_store
from app.llm.cost import session_totals
from app.llm.registry import available_models

router = APIRouter(prefix="/api", tags=["chat"])


class NewSession(BaseModel):
    title: str | None = None
    model_id: str | None = None


class Advance(BaseModel):
    action: str = Field(description="message | approve_tables | approve_plan | approve_code | reset_to")
    text: str | None = None
    table_ids: list[str] | None = None
    model_id: str | None = None
    stage: str | None = None


class TableSelection(BaseModel):
    table_ids: list[str]


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


@router.get("/models")
def models() -> dict:
    return {"models": available_models()}


@router.post("/sessions")
def create_session(body: NewSession) -> dict:
    store = get_store()
    state: dict[str, Any] = {"model_id": body.model_id}
    session_id = store.create_session(body.title or "New project", state_machine.INTAKE, state)
    return {"id": session_id, "stage": state_machine.INTAKE, "state": state}


@router.get("/sessions")
def list_sessions() -> dict:
    return {"sessions": get_store().list_sessions()}


@router.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    store = get_store()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, f"Unknown session {session_id}")
    return {
        **session,
        "messages": store.get_messages(session_id),
        "usage": session_totals(session_id),
        "stages": state_machine.STAGE_ORDER,
    }


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> dict:
    get_store().delete_session(session_id)
    return {"deleted": session_id}


@router.put("/sessions/{session_id}/tables")
def set_tables(session_id: str, body: TableSelection) -> dict:
    """Table checkbox changes during review. Deliberately not an LLM call -
    the analyst is correcting retrieval, not asking a question."""
    store = get_store()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, f"Unknown session {session_id}")
    state = session["state"]
    state["selected_tables"] = body.table_ids
    store.update_session(session_id, state=state)
    return {"selected_tables": body.table_ids}


@router.post("/sessions/{session_id}/advance")
async def advance(session_id: str, body: Advance) -> StreamingResponse:
    store = get_store()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, f"Unknown session {session_id}")

    if body.action == "message" and body.text:
        store.add_message(session_id, "user", body.text, stage=session["stage"])
    elif body.action.startswith("approve_"):
        label = {
            "approve_tables": "Approved the selected tables.",
            "approve_plan": "Approved the execution plan.",
            "approve_code": "Approved the generated code.",
        }.get(body.action, body.action)
        store.add_message(session_id, "user", label, stage=session["stage"], meta={"gate": body.action})

    payload = body.model_dump(exclude_none=True)

    async def stream() -> AsyncIterator[str]:
        try:
            async for event in state_machine.run(session_id, body.action, payload):
                yield sse(event)
        except Exception as exc:  # noqa: BLE001 - the stream is the only channel back
            yield sse({"type": "error", "message": str(exc)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{session_id}/artifacts/{kind}")
def get_artifact(session_id: str, kind: str) -> dict:
    artifact = get_store().latest_artifact(session_id, kind)
    if artifact is None:
        raise HTTPException(404, f"No {kind} artifact for session {session_id}")
    return artifact


@router.get("/sessions/{session_id}/artifacts/{kind}/history")
def artifact_history(session_id: str, kind: str) -> dict:
    return {"versions": get_store().artifact_history(session_id, kind)}
