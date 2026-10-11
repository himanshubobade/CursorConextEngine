"""Publishing the approved artifacts."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agent import state_machine
from app.context.store import get_store
from app.delivery import publisher

router = APIRouter(prefix="/api/publish", tags=["publish"])


class PublishRequest(BaseModel):
    branch: str | None = None
    to_git: bool = True
    to_workspace: bool = True


def _approved_code(session_id: str) -> tuple[dict, dict]:
    store = get_store()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, f"Unknown session {session_id}")
    code = session["state"].get("code")
    if not code:
        raise HTTPException(409, "This session has no generated code yet.")
    return session, code


@router.get("/{session_id}/preview")
def preview(session_id: str) -> dict:
    _, code = _approved_code(session_id)
    return publisher.preview(
        code.get("project_name") or "generated_project",
        code.get("config_yaml", ""),
        code.get("notebook_py", ""),
    )


@router.post("/{session_id}")
def publish(session_id: str, body: PublishRequest) -> dict:
    store = get_store()
    session, code = _approved_code(session_id)

    validation = code.get("validation") or {}
    if not validation.get("ok", False):
        raise HTTPException(
            409,
            "The generated code has not passed validation. Ask for a revision before publishing.",
        )

    project_name = code.get("project_name") or "generated_project"
    config_yaml = code.get("config_yaml", "")
    notebook_py = code.get("notebook_py", "")

    result: dict = {"project_name": project_name, "ok": False}
    if body.to_git:
        result["git"] = publisher.publish_to_git(project_name, config_yaml, notebook_py, body.branch).__dict__
    if body.to_workspace:
        result["workspace"] = publisher.publish_to_workspace(project_name, config_yaml, notebook_py)
    result["ok"] = bool(
        (result.get("git") or {}).get("ok") or (result.get("workspace") or {}).get("ok")
    )

    state = session["state"]
    state["publish"] = result
    store.update_session(session_id, state=state)
    store.save_artifact(session_id, "publish", result, approved=result["ok"])

    summary_parts = []
    if "git" in result:
        summary_parts.append("Git: " + (result["git"].get("detail") or ""))
    if "workspace" in result:
        summary_parts.append("Workspace: " + (result["workspace"].get("detail") or ""))
    store.add_message(
        session_id,
        "assistant",
        "\n\n".join(p for p in summary_parts if p.strip()),
        stage=state_machine.PUBLISH_READY,
        meta={"event": "publish"},
    )
    return result
