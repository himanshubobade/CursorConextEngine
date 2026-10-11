"""Creates and runs the Databricks job for a published project.

The task, notebook path and parameters still come from the framework's
`jobs/job_template.json`. Compute does not: the template declares a classic
job cluster (14.3, Standard_DS4_v2, 1-4 workers), and ContextEngine rewrites
that to serverless by default so the job does not wait on a cluster to start.
Classic remains available as an override.
"""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.context.databricks_client import DatabricksError, get_client
from app.context.framework_spec import CONFIG_FILENAME, job_template, load_config_module

log = logging.getLogger(__name__)

SERVERLESS = "serverless"
CLASSIC = "classic"
VALID_COMPUTE = {SERVERLESS, CLASSIC}
VALID_PERFORMANCE = {"STANDARD", "PERFORMANCE_OPTIMIZED"}


class DeployError(RuntimeError):
    pass


def parse_config(config_yaml: str):
    """Load the YAML through the framework's own loader, so defaults like
    `timezone_id: Asia/Kolkata` and `max_workers: 4` come from the framework
    rather than being duplicated here."""
    module = load_config_module()
    with tempfile.TemporaryDirectory(prefix="contextengine_deploy_") as tmp:
        path = Path(tmp) / CONFIG_FILENAME
        path.write_text(config_yaml, encoding="utf-8")
        return module.load_ingestion_config(str(path))


def _base_payload(project_name: str, repo_root: str, cluster_policy_id: str) -> dict:
    template = json.dumps(job_template())
    filled = (
        template.replace("{project_name}", project_name)
        .replace("{repo_root}", repo_root)
        .replace("{cluster_policy_id}", cluster_policy_id)
    )
    return json.loads(filled)


def _apply_serverless(payload: dict, performance_target: str) -> dict:
    """Notebook tasks with no cluster attached run on Jobs serverless.

    The template's job_clusters / job_cluster_key are dropped rather than
    left empty: an empty new_cluster is rejected by the API.
    """
    payload.pop("job_clusters", None)
    for task in payload.get("tasks", []):
        task.pop("job_cluster_key", None)
        task.pop("new_cluster", None)
        task.pop("existing_cluster_id", None)
    payload["performance_target"] = performance_target
    return payload


def _apply_classic(payload: dict, cluster_policy_id: str, max_workers: int | None) -> dict:
    if not cluster_policy_id:
        for cluster in payload.get("job_clusters", []):
            cluster.get("new_cluster", {}).pop("policy_id", None)
    if max_workers and payload.get("job_clusters"):
        payload["job_clusters"][0]["new_cluster"]["autoscale"]["max_workers"] = max_workers
    return payload


def build_job_payload(
    project_name: str,
    config,
    repo_root: str,
    cluster_policy_id: str,
    compute: str = SERVERLESS,
    performance_target: str = "STANDARD",
    attach_schedule: bool = True,
    on_failure_emails: list[str] | None = None,
    max_workers: int | None = None,
) -> dict:
    payload = _base_payload(project_name, repo_root, cluster_policy_id)
    compute = (compute or SERVERLESS).lower()
    if compute not in VALID_COMPUTE:
        raise DeployError(f"Unknown compute '{compute}'. Use serverless or classic.")
    performance_target = (performance_target or "STANDARD").upper()
    if performance_target not in VALID_PERFORMANCE:
        raise DeployError(
            f"Unknown performance_target '{performance_target}'. Use STANDARD or PERFORMANCE_OPTIMIZED."
        )

    schedule = getattr(config, "schedule", None) if attach_schedule else None
    emails = on_failure_emails
    workers = max_workers
    if schedule:
        payload["schedule"] = {
            "quartz_cron_expression": schedule.quartz_cron_expression,
            "timezone_id": schedule.timezone_id,
            "pause_status": "UNPAUSED" if getattr(config, "enabled", True) else "PAUSED",
        }
        if workers is None:
            workers = schedule.max_workers
        if not emails and schedule.on_failure_emails:
            emails = schedule.on_failure_emails
    else:
        payload.pop("schedule", None)

    if emails:
        payload.setdefault("email_notifications", {})["on_failure"] = emails

    if compute == SERVERLESS:
        _apply_serverless(payload, performance_target)
    else:
        _apply_classic(payload, cluster_policy_id, workers)

    payload["_contextengine"] = {
        "compute": compute,
        "performance_target": performance_target if compute == SERVERLESS else None,
        "run_mode": "scheduled" if payload.get("schedule") else "once",
    }
    return payload


def _public_payload(payload: dict) -> dict:
    """Strip the internal annotation before sending to Databricks."""
    clean = dict(payload)
    clean.pop("_contextengine", None)
    return clean


def deploy(
    project_name: str,
    config_yaml: str,
    compute: str | None = None,
    performance_target: str | None = None,
    attach_schedule: bool = True,
    on_failure_emails: list[str] | None = None,
    max_workers: int | None = None,
) -> dict:
    """Create the job, or overwrite it if a job with this name already
    exists. Names are deterministic (`ingestion_<project>`), which is what
    makes redeploying a schedule change safe. jobs/reset is a full overwrite,
    so switching from cron to run-once (or serverless to classic) replaces
    the previous settings rather than merging with them.
    """
    settings = get_settings()
    client = get_client()
    if not client.token:
        raise DeployError("No Databricks token configured.")

    config = parse_config(config_yaml)
    repo_root = settings.workspace_repo_root.rstrip("/")
    policy_id = settings.get("databricks", "cluster_policy_id", default="") or ""
    compute = compute or settings.get("databricks", "job_compute", default=SERVERLESS) or SERVERLESS
    performance_target = (
        performance_target
        or settings.get("databricks", "serverless_performance_target", default="STANDARD")
        or "STANDARD"
    )
    payload = build_job_payload(
        project_name,
        config,
        repo_root,
        policy_id,
        compute=compute,
        performance_target=performance_target,
        attach_schedule=attach_schedule,
        on_failure_emails=on_failure_emails,
        max_workers=max_workers,
    )
    job_name = payload["name"]
    meta = payload.get("_contextengine") or {}
    api_payload = _public_payload(payload)

    try:
        existing = client.find_job_id_by_name(job_name)
        if existing:
            client.reset_job(existing, api_payload)
            job_id, created = existing, False
        else:
            job_id = client.create_job(api_payload)
            created = True
    except DatabricksError as exc:
        raise DeployError(str(exc)) from exc

    compute_label = (
        f"serverless ({meta.get('performance_target', 'STANDARD').lower()})"
        if meta.get("compute") == SERVERLESS
        else "classic job cluster"
    )
    mode_label = "unscheduled (run once / manual)" if not payload.get("schedule") else "on a cron schedule"
    return {
        "job_id": job_id,
        "job_name": job_name,
        "created": created,
        "job_url": client.job_url(job_id),
        "schedule": payload.get("schedule"),
        "compute": meta.get("compute"),
        "performance_target": meta.get("performance_target"),
        "run_mode": meta.get("run_mode"),
        "notebook_path": payload["tasks"][0]["notebook_task"]["notebook_path"],
        "detail": (
            f"{'Created' if created else 'Updated'} job '{job_name}' on {compute_label}, {mode_label}."
        ),
    }


def run_now(job_id: int) -> dict:
    client = get_client()
    try:
        run_id = client.run_job_now(job_id)
    except DatabricksError as exc:
        raise DeployError(str(exc)) from exc
    return {"run_id": run_id, "run_url": client.run_url(job_id, run_id)}


def run_status(run_id: int) -> dict:
    client = get_client()
    try:
        run = client.get_run(run_id)
    except DatabricksError as exc:
        raise DeployError(str(exc)) from exc
    state = run.get("state", {}) or run.get("status", {})
    return {
        "run_id": run_id,
        "life_cycle_state": state.get("life_cycle_state") or state.get("state"),
        "result_state": state.get("result_state"),
        "state_message": state.get("state_message"),
        "start_time": run.get("start_time"),
        "end_time": run.get("end_time"),
        "run_page_url": run.get("run_page_url"),
    }


# ----------------------------------------------------------------------
# the metadata flywheel
# ----------------------------------------------------------------------


def apply_column_comments(table: str, columns: list[dict[str, Any]]) -> dict:
    """Write the plan's column descriptions into Unity Catalog.

    This is what closes the loop: the descriptions this project generated
    become part of the snapshot on the next sync, so the next analyst's
    search can find this table by meaning rather than by name.
    """
    client = get_client()
    if not client.token:
        raise DeployError("No Databricks token configured.")

    parts = table.split(".")
    if len(parts) != 3:
        raise DeployError(f"Expected a three-level table name, got '{table}'.")

    applied, failed = [], []
    for column in columns:
        name = column.get("name")
        description = (column.get("description") or "").strip()
        if not name or not description:
            continue
        escaped = description.replace("'", "''")
        statement = (
            f"ALTER TABLE `{parts[0]}`.`{parts[1]}`.`{parts[2]}` "
            f"ALTER COLUMN `{name}` COMMENT '{escaped}'"
        )
        try:
            client.execute(statement)
            applied.append(name)
        except DatabricksError as exc:
            failed.append({"column": name, "error": str(exc)})

    return {
        "table": table,
        "applied": applied,
        "failed": failed,
        "detail": f"Applied {len(applied)} column comment(s)"
        + (f", {len(failed)} failed." if failed else "."),
    }
