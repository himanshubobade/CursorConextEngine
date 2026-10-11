"""Job creation, manual runs, and writing column comments back to Unity Catalog."""
from __future__ import annotations

import yaml
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agent import state_machine
from app.context.store import get_store
from app.delivery import deployer

router = APIRouter(prefix="/api/deploy", tags=["deploy"])


class DeployRequest(BaseModel):
    # scheduled = cron on the job. once = no schedule; run it manually or
    # immediately. jobs/reset overwrites, so switching modes is safe.
    run_mode: Literal["scheduled", "once"] = "scheduled"
    quartz_cron_expression: str | None = None
    timezone_id: str | None = None
    max_workers: int | None = None
    on_failure_emails: list[str] | None = None
    enabled: bool = True
    run_now: bool = False
    compute: Literal["serverless", "classic"] | None = None
    performance_target: Literal["STANDARD", "PERFORMANCE_OPTIMIZED"] | None = None


def _session_code(session_id: str) -> tuple[dict, dict]:
    session = get_store().get_session(session_id)
    if session is None:
        raise HTTPException(404, f"Unknown session {session_id}")
    code = session["state"].get("code")
    if not code:
        raise HTTPException(409, "This session has no generated code yet.")
    return session, code


def _apply_schedule(config_yaml: str, body: DeployRequest) -> str:
    """Fold the analyst's schedule choices back into the YAML.

    The job and the committed config must agree. A one-shot deploy strips
    the schedule block so a later CD pass will not recreate a cron that
    the analyst turned off.
    """
    config = yaml.safe_load(config_yaml) or {}
    if body.run_mode == "once":
        config.pop("schedule", None)
        config["enabled"] = True
        return yaml.safe_dump(config, sort_keys=False, default_flow_style=False)

    schedule = dict(config.get("schedule") or {})
    if body.quartz_cron_expression:
        schedule["quartz_cron_expression"] = body.quartz_cron_expression
    if body.timezone_id:
        schedule["timezone_id"] = body.timezone_id
    if body.max_workers:
        schedule["max_workers"] = body.max_workers
    if body.on_failure_emails is not None:
        schedule["on_failure_emails"] = body.on_failure_emails
    if schedule:
        config["schedule"] = schedule
    config["enabled"] = body.enabled
    return yaml.safe_dump(config, sort_keys=False, default_flow_style=False)


@router.post("/{session_id}")
def deploy(session_id: str, body: DeployRequest) -> dict:
    store = get_store()
    session, code = _session_code(session_id)
    published = session["state"].get("publish")
    if not published or not published.get("ok"):
        raise HTTPException(409, "Publish the project before deploying - the job points at the workspace copy.")

    project_name = code.get("project_name") or "generated_project"
    config_yaml = _apply_schedule(code.get("config_yaml", ""), body)

    # Keep the on-disk and in-workspace config in step with the job.
    if config_yaml != code.get("config_yaml"):
        from app.delivery import publisher

        code["config_yaml"] = config_yaml
        session["state"]["code"] = code
        store.update_session(session_id, state=session["state"])
        publisher.publish_to_workspace(project_name, config_yaml, code.get("notebook_py", ""))
        publisher.publish_to_git(project_name, config_yaml, code.get("notebook_py", ""))

    try:
        result = deployer.deploy(
            project_name,
            config_yaml,
            compute=body.compute,
            performance_target=body.performance_target,
            attach_schedule=body.run_mode == "scheduled",
            on_failure_emails=body.on_failure_emails,
            max_workers=body.max_workers,
        )
    except deployer.DeployError as exc:
        raise HTTPException(502, str(exc)) from exc

    if body.run_now:
        try:
            result.update(deployer.run_now(result["job_id"]))
        except deployer.DeployError as exc:
            result["run_error"] = str(exc)

    store.save_deployment(
        session_id,
        project_name,
        job_id=result["job_id"],
        job_url=result.get("job_url"),
        last_run_id=result.get("run_id"),
        status="deployed",
    )
    session["state"]["deploy"] = result
    store.update_session(session_id, stage=state_machine.DEPLOYED, state=session["state"])
    store.save_artifact(session_id, "deploy", result, approved=True)
    store.add_message(
        session_id,
        "assistant",
        result["detail"] + (f" Started run {result['run_id']}." if result.get("run_id") else ""),
        stage=state_machine.DEPLOYED,
        meta={"event": "deploy"},
    )
    return result


@router.post("/{session_id}/run")
def run(session_id: str) -> dict:
    store = get_store()
    session, code = _session_code(session_id)
    deployment = session["state"].get("deploy")
    if not deployment:
        raise HTTPException(409, "Deploy the project before running it.")
    try:
        result = deployer.run_now(deployment["job_id"])
    except deployer.DeployError as exc:
        raise HTTPException(502, str(exc)) from exc
    store.save_deployment(session_id, code.get("project_name", ""), last_run_id=result["run_id"], status="running")
    return result


@router.get("/runs/{run_id}")
def run_state(run_id: int) -> dict:
    try:
        return deployer.run_status(run_id)
    except deployer.DeployError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/{session_id}/status")
def status(session_id: str) -> dict:
    session, code = _session_code(session_id)
    deployment = get_store().get_deployment(code.get("project_name") or "")
    state = session["state"].get("deploy")
    latest = None
    if deployment and deployment.get("last_run_id"):
        try:
            latest = deployer.run_status(deployment["last_run_id"])
        except deployer.DeployError as exc:
            latest = {"error": str(exc)}
    return {"deployment": deployment, "job": state, "latest_run": latest}


@router.post("/{session_id}/column-comments")
def column_comments(session_id: str) -> dict:
    """Write the plan's column descriptions onto the new table."""
    session, code = _session_code(session_id)
    plan = session["state"].get("plan") or {}
    table = plan.get("sink_table")
    columns = plan.get("sink_columns") or []
    if not table:
        raise HTTPException(409, "The plan does not name a sink table.")
    if not columns:
        raise HTTPException(409, "The plan has no column descriptions to apply.")
    try:
        result = deployer.apply_column_comments(table, columns)
    except deployer.DeployError as exc:
        raise HTTPException(502, str(exc)) from exc

    get_store().add_message(
        session_id,
        "assistant",
        result["detail"] + " These descriptions feed the catalog search, so the next project can find this table.",
        stage=state_machine.DEPLOYED,
        meta={"event": "column_comments"},
    )
    return result
