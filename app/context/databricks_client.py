"""Databricks access layer.

SQL goes through the Statement Execution REST API rather than a Thrift/ODBC
driver - plain HTTPS + JSON, no compiled dependencies and no client/server
protocol handshake to go stale. Ported from the uc-lineage-web POC and
extended with the Workspace and Jobs endpoints the publish/deploy stages need.
"""
from __future__ import annotations

import base64
import re
import time
from functools import lru_cache
from typing import Any, Iterable

import requests

from app.config import get_settings


class DatabricksError(RuntimeError):
    """Any non-2xx response or failed statement, with the workspace's own
    error message attached instead of a bare HTTPError."""


class DatabricksClient:
    def __init__(self, hostname: str, http_path: str, token: str) -> None:
        self.hostname = hostname.replace("https://", "").rstrip("/")
        self.http_path = http_path
        # The Statement Execution API wants the warehouse id, not the
        # ODBC/JDBC http_path, but they overlap:
        # ".../sql/1.0/warehouses/<warehouse_id>" -> take the tail.
        self.warehouse_id = http_path.rstrip("/").split("/")[-1] if http_path else ""
        self.token = token
        # Azure Databricks embeds the numeric workspace id in the hostname
        # (adb-<workspace_id>.<n>.azuredatabricks.net), which is the same value
        # every workspace URL carries as "?o=" - so deep links need no lookup.
        match = re.match(r"adb-(\d+)\.", self.hostname)
        self.workspace_id = match.group(1) if match else None

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"https://{self.hostname}"

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _check(self, resp: requests.Response) -> None:
        if resp.ok:
            return
        try:
            body = resp.json()
            detail = body.get("message") or body.get("error_code") or resp.text
        except ValueError:
            detail = resp.text
        raise DatabricksError(f"Databricks API error {resp.status_code} for {resp.url}: {detail}")

    def rest_get(self, path: str, params: dict | None = None, timeout: int = 30) -> dict:
        resp = requests.get(f"{self.base_url}{path}", headers=self.headers, params=params, timeout=timeout)
        self._check(resp)
        return resp.json()

    def rest_post(self, path: str, payload: dict, timeout: int = 60) -> dict:
        resp = requests.post(f"{self.base_url}{path}", headers=self.headers, json=payload, timeout=timeout)
        self._check(resp)
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------
    # SQL
    # ------------------------------------------------------------------

    def run_query(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        wait_timeout: str = "30s",
        row_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Run SQL and return a list of row dicts.

        `query` uses named `:param` placeholders; `params` supplies the values.
        Chunked results are followed to completion.
        """
        if not self.token:
            raise DatabricksError("DATABRICKS_TOKEN is not configured.")
        if not self.warehouse_id:
            raise DatabricksError("DATABRICKS_HTTP_PATH is not configured (no warehouse id).")

        statements_url = f"{self.base_url}/api/2.0/sql/statements"
        payload: dict[str, Any] = {
            "warehouse_id": self.warehouse_id,
            "statement": query,
            "wait_timeout": wait_timeout,
            "format": "JSON_ARRAY",
            "disposition": "INLINE",
        }
        if params:
            payload["parameters"] = [
                {"name": k, "value": str(v), "type": "STRING"} for k, v in params.items()
            ]
        if row_limit:
            payload["row_limit"] = row_limit

        resp = requests.post(statements_url, headers=self.headers, json=payload, timeout=120)
        self._check(resp)
        data = resp.json()
        statement_id = data["statement_id"]

        # wait_timeout already blocks server-side; this only covers the rare
        # case of a cold warehouse taking longer than that.
        while data["status"]["state"] in ("PENDING", "RUNNING"):
            time.sleep(1)
            resp = requests.get(f"{statements_url}/{statement_id}", headers=self.headers, timeout=60)
            self._check(resp)
            data = resp.json()

        state = data["status"]["state"]
        if state != "SUCCEEDED":
            err = data["status"].get("error", {})
            raise DatabricksError(f"Query failed ({state}): {err.get('message', 'unknown error')}")

        columns = [c["name"] for c in data["manifest"]["schema"]["columns"]]
        result = data.get("result", {}) or {}
        rows = list(result.get("data_array", []) or [])

        while result.get("next_chunk_internal_link"):
            resp = requests.get(
                f"{self.base_url}{result['next_chunk_internal_link']}",
                headers=self.headers,
                timeout=60,
            )
            self._check(resp)
            result = resp.json()
            rows.extend(result.get("data_array", []) or [])

        return [dict(zip(columns, row)) for row in rows]

    def execute(self, statement: str) -> None:
        """Fire-and-check a DDL/DML statement that returns no useful rows."""
        self.run_query(statement)

    # ------------------------------------------------------------------
    # identity / catalog discovery
    # ------------------------------------------------------------------

    def current_user(self) -> dict:
        return self.rest_get("/api/2.0/preview/scim/v2/Me", timeout=15)

    def list_catalogs(self) -> list[str]:
        """Catalog names visible to this token, via Unity Catalog REST (which
        needs no warehouse) with a SQL fallback."""
        try:
            payload = self.rest_get("/api/2.1/unity-catalog/catalogs", timeout=30)
            names = [c["name"] for c in payload.get("catalogs", [])]
            if names:
                return names
        except DatabricksError:
            pass
        return [r["catalog"] for r in self.run_query("SHOW CATALOGS")]

    # ------------------------------------------------------------------
    # workspace files
    # ------------------------------------------------------------------

    def mkdirs(self, path: str) -> None:
        self.rest_post("/api/2.0/workspace/mkdirs", {"path": path})

    def upload_file(self, path: str, content: str, language: str | None = None) -> None:
        """Write a workspace file, creating the parent directory first.

        `language` is set for Python notebook sources so Databricks renders
        them as notebooks; plain files (YAML) are imported with format AUTO.
        """
        parent = path.rsplit("/", 1)[0]
        if parent:
            self.mkdirs(parent)
        payload: dict[str, Any] = {
            "path": path,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "overwrite": True,
            "format": "SOURCE" if language else "AUTO",
        }
        if language:
            payload["language"] = language
        self.rest_post("/api/2.0/workspace/import", payload)

    def workspace_delete(self, path: str) -> bool:
        """Delete one workspace object. Returns False if it did not exist."""
        try:
            self.rest_post("/api/2.0/workspace/delete", {"path": path, "recursive": False})
            return True
        except DatabricksError as exc:
            if "RESOURCE_DOES_NOT_EXIST" in str(exc) or "404" in str(exc):
                return False
            raise

    def workspace_list(self, path: str) -> list[dict]:
        try:
            return self.rest_get("/api/2.0/workspace/list", {"path": path}).get("objects", [])
        except DatabricksError:
            return []

    # ------------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------------

    def find_job_id_by_name(self, name: str) -> int | None:
        payload = self.rest_get("/api/2.1/jobs/list", {"name": name, "limit": 25})
        for job in payload.get("jobs", []):
            if job.get("settings", {}).get("name") == name:
                return job["job_id"]
        return None

    def create_job(self, settings_payload: dict) -> int:
        return self.rest_post("/api/2.1/jobs/create", settings_payload)["job_id"]

    def reset_job(self, job_id: int, settings_payload: dict) -> None:
        self.rest_post("/api/2.1/jobs/reset", {"job_id": job_id, "new_settings": settings_payload})

    def run_job_now(self, job_id: int) -> int:
        return self.rest_post("/api/2.1/jobs/run-now", {"job_id": job_id})["run_id"]

    def get_run(self, run_id: int) -> dict:
        return self.rest_get("/api/2.1/jobs/runs/get", {"run_id": run_id})

    # ------------------------------------------------------------------
    # deep links
    # ------------------------------------------------------------------

    def asset_url(self, entity_type: str | None, entity_id: str | None) -> str | None:
        if not entity_type or not entity_id or not self.workspace_id:
            return None
        suffix = f"?o={self.workspace_id}"
        routes = {
            "NOTEBOOK": f"/editor/notebooks/{entity_id}",
            "JOB": f"/jobs/{entity_id}",
            "DBSQL_QUERY": f"/sql/editor/{entity_id}",
            "PIPELINE": f"/pipelines/{entity_id}",
            "DASHBOARD": f"/sql/dashboards/{entity_id}",
        }
        route = routes.get(entity_type)
        return f"{self.base_url}{route}{suffix}" if route else None

    def job_url(self, job_id: int) -> str | None:
        if not self.workspace_id:
            return f"{self.base_url}/jobs/{job_id}"
        return f"{self.base_url}/jobs/{job_id}?o={self.workspace_id}"

    def run_url(self, job_id: int, run_id: int) -> str:
        suffix = f"?o={self.workspace_id}" if self.workspace_id else ""
        return f"{self.base_url}/jobs/{job_id}/runs/{run_id}{suffix}"

    @lru_cache(maxsize=512)
    def resolve_asset_name(self, entity_type: str | None, entity_id: str | None) -> str:
        """Friendly name for a lineage entity. Best-effort: falls back to
        '<TYPE> #<id>' rather than failing a whole lineage response."""
        if not entity_type or entity_type == "AD_HOC":
            return "Ad hoc query"
        try:
            if entity_type == "JOB":
                payload = self.rest_get("/api/2.1/jobs/get", {"job_id": entity_id}, timeout=10)
                name = payload.get("settings", {}).get("name")
                if name:
                    return name
            elif entity_type == "PIPELINE":
                name = self.rest_get(f"/api/2.0/pipelines/{entity_id}", timeout=10).get("name")
                if name:
                    return name
            elif entity_type == "NOTEBOOK":
                # Lineage carries workspace object ids and there is no generic
                # "get object by id" endpoint, so the id is the best label.
                return f"Notebook #{entity_id}"
            elif entity_type == "DBSQL_QUERY":
                return f"SQL query #{str(entity_id)[:8]}"
            elif entity_type == "DASHBOARD":
                return f"Dashboard #{str(entity_id)[:8]}"
        except (DatabricksError, requests.RequestException):
            pass
        return f"{entity_type} #{entity_id}"


@lru_cache(maxsize=1)
def get_client() -> DatabricksClient:
    settings = get_settings()
    return DatabricksClient(
        hostname=settings.secret("databricks", "server_hostname") or "",
        http_path=settings.secret("databricks", "http_path") or "",
        token=settings.secret("databricks", "token") or "",
    )


def reset_client() -> None:
    get_client.cache_clear()


def chunked(items: Iterable, size: int):
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
