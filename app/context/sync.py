"""Snapshots Unity Catalog metadata and lineage into the local store.

This is the only place the SQL warehouse gets touched for context. Once a
sync has run, retrieval, lineage traversal and the projected-lineage preview
are all local and free.

Run from the UI, or standalone:

    python -m app.context.sync                 # every readable catalog
    python -m app.context.sync main other      # named catalogs only
    python -m app.context.sync --no-embed      # snapshot only
"""
from __future__ import annotations

import logging
import re
import sys
import time
from datetime import date, timedelta
from typing import Callable

from app.config import get_settings
from app.context.databricks_client import DatabricksClient, DatabricksError, get_client
from app.context.embeddings import refresh_index
from app.context.store import Store, get_store, table_id

log = logging.getLogger(__name__)

Progress = Callable[[str, dict], None]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_\-]+$")


def _safe_identifier(name: str) -> str:
    """Catalog names are interpolated into SQL (they cannot be bound as
    parameters), so anything unexpected is rejected rather than escaped."""
    if not _IDENTIFIER.match(name):
        raise ValueError(f"Refusing to interpolate unusual catalog name into SQL: {name!r}")
    return name


def resolve_catalogs(client: DatabricksClient, requested: list[str] | None = None) -> list[str]:
    settings = get_settings()
    skip = set(settings.get("databricks", "skip_catalogs", default=[]) or [])
    if requested:
        return [c for c in requested if c not in skip]
    configured = settings.get("databricks", "catalogs", default=[]) or []
    if configured:
        return [c for c in configured if c not in skip]
    return [c for c in client.list_catalogs() if c not in skip]


# ----------------------------------------------------------------------
# per-catalog metadata
# ----------------------------------------------------------------------


def fetch_tables(client: DatabricksClient, catalog: str) -> list[dict]:
    cat = _safe_identifier(catalog)
    try:
        rows = client.run_query(
            f"""
            SELECT table_schema, table_name, table_type, table_owner, comment
            FROM `{cat}`.information_schema.tables
            WHERE table_type IN ('MANAGED', 'EXTERNAL', 'VIEW')
            """
        )
    except DatabricksError:
        # Older metastores don't expose table_owner/comment here.
        rows = client.run_query(
            f"""
            SELECT table_schema, table_name, table_type
            FROM `{cat}`.information_schema.tables
            WHERE table_type IN ('MANAGED', 'EXTERNAL', 'VIEW')
            """
        )
    return [
        {
            "schema_name": r["table_schema"],
            "table_name": r["table_name"],
            "table_type": r.get("table_type"),
            "owner": r.get("table_owner"),
            "comment": r.get("comment"),
        }
        for r in rows
    ]


def fetch_columns(client: DatabricksClient, catalog: str) -> list[dict]:
    cat = _safe_identifier(catalog)
    try:
        rows = client.run_query(
            f"""
            SELECT table_schema, table_name, column_name, full_data_type, comment,
                   ordinal_position, is_nullable
            FROM `{cat}`.information_schema.columns
            """
        )
        type_key = "full_data_type"
    except DatabricksError:
        rows = client.run_query(
            f"""
            SELECT table_schema, table_name, column_name, data_type, comment, ordinal_position
            FROM `{cat}`.information_schema.columns
            """
        )
        type_key = "data_type"
    return [
        {
            "schema_name": r["table_schema"],
            "table_name": r["table_name"],
            "column_name": r["column_name"],
            "data_type": r.get(type_key),
            "comment": r.get("comment"),
            "ordinal": int(r["ordinal_position"]) if r.get("ordinal_position") is not None else None,
            "nullable": r.get("is_nullable"),
        }
        for r in rows
    ]


# ----------------------------------------------------------------------
# lineage
# ----------------------------------------------------------------------


def fetch_lineage(client: DatabricksClient, catalogs: list[str], since: str) -> list[dict]:
    """Table-to-table lineage rows since `since`, pre-aggregated in the
    warehouse so we transfer one row per distinct edge+asset rather than one
    row per execution."""
    quoted = ", ".join(f"'{_safe_identifier(c)}'" for c in catalogs)
    rows = client.run_query(
        f"""
        SELECT
            source_table_catalog, source_table_schema, source_table_name,
            target_table_catalog, target_table_schema, target_table_name,
            COALESCE(entity_type, '') AS entity_type,
            COALESCE(CAST(entity_id AS STRING), '') AS entity_id,
            COUNT(*) AS occurrences,
            CAST(MAX(event_time) AS STRING) AS last_event
        FROM system.access.table_lineage
        WHERE event_date >= :since
          AND source_table_name IS NOT NULL
          AND target_table_name IS NOT NULL
          AND (source_table_catalog IN ({quoted}) OR target_table_catalog IN ({quoted}))
        GROUP BY
            source_table_catalog, source_table_schema, source_table_name,
            target_table_catalog, target_table_schema, target_table_name,
            COALESCE(entity_type, ''), COALESCE(CAST(entity_id AS STRING), '')
        """,
        {"since": since},
    )
    edges = []
    for r in rows:
        source = table_id(r["source_table_catalog"], r["source_table_schema"], r["source_table_name"])
        target = table_id(r["target_table_catalog"], r["target_table_schema"], r["target_table_name"])
        if source == target:
            continue  # e.g. MERGE into itself - nothing to draw
        edges.append(
            {
                "source_id": source,
                "target_id": target,
                "entity_type": r["entity_type"],
                "entity_id": r["entity_id"],
                "occurrences": int(r["occurrences"] or 1),
                "last_event": r["last_event"],
            }
        )
    return edges


# ----------------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------------


def run_sync(
    catalogs: list[str] | None = None,
    embed: bool = True,
    full_lineage: bool = False,
    progress: Progress | None = None,
    store: Store | None = None,
) -> dict:
    store = store or get_store()
    client = get_client()
    settings = get_settings()
    emit = progress or (lambda *_: None)
    started = time.time()

    emit("start", {"message": "Resolving catalogs"})
    targets = resolve_catalogs(client, catalogs)
    if not targets:
        raise DatabricksError("No catalogs to sync. Check databricks.catalogs in settings.yaml and token permissions.")
    emit("catalogs", {"catalogs": targets})

    report: dict = {"catalogs": {}, "errors": []}
    total_tables = total_columns = 0

    for catalog in targets:
        emit("catalog", {"catalog": catalog, "phase": "tables"})
        try:
            tables = fetch_tables(client, catalog)
            store.replace_catalog_tables(catalog, tables)

            emit("catalog", {"catalog": catalog, "phase": "columns", "tables": len(tables)})
            columns = fetch_columns(client, catalog)
            store.replace_catalog_columns(catalog, columns)

            report["catalogs"][catalog] = {"tables": len(tables), "columns": len(columns)}
            total_tables += len(tables)
            total_columns += len(columns)
        except (DatabricksError, ValueError) as exc:
            log.warning("catalog %s failed: %s", catalog, exc)
            report["errors"].append({"catalog": catalog, "error": str(exc)})
            emit("catalog_error", {"catalog": catalog, "error": str(exc)})

    synced_catalogs = [c for c in targets if c in report["catalogs"]]

    # Lineage
    lookback = int(settings.get("databricks", "lineage_lookback_days", default=90))
    watermark = store.get_state("lineage_synced_through")
    default_since = (date.today() - timedelta(days=lookback)).isoformat()
    since = default_since if (full_lineage or not watermark) else max(watermark, default_since)

    emit("lineage", {"since": since, "phase": "query"})
    try:
        if full_lineage or not watermark:
            store.clear_lineage()
        edges = fetch_lineage(client, synced_catalogs, since) if synced_catalogs else []
        store.upsert_lineage_edges(edges)
        pruned = store.prune_lineage_to_known_tables()
        store.set_state("lineage_synced_through", date.today().isoformat())
        report["lineage"] = {"edges_seen": len(edges), "pruned": pruned, "since": since}
        emit("lineage", {"edges": len(edges), "pruned": pruned, "phase": "done"})
    except DatabricksError as exc:
        # A workspace without `GRANT SELECT ON system.access.table_lineage`
        # should still get a usable metadata snapshot.
        log.warning("lineage sync failed: %s", exc)
        report["errors"].append({"stage": "lineage", "error": str(exc)})
        report["lineage"] = {"edges_seen": 0, "error": str(exc)}
        emit("lineage_error", {"error": str(exc)})

    # Documents + embeddings
    if embed:
        emit("index", {"phase": "start"})
        try:
            report["index"] = refresh_index(store, progress=lambda stage, info: emit(f"index.{stage}", info))
        except Exception as exc:  # noqa: BLE001 - embeddings are optional for a snapshot-only sync
            log.warning("embedding refresh failed: %s", exc)
            report["errors"].append({"stage": "embeddings", "error": str(exc)})
            emit("index_error", {"error": str(exc)})
    else:
        from app.context.documents import build_documents

        docs = build_documents(store)
        store.upsert_docs(docs)
        report["index"] = {"documents": len(docs), "embedded": 0, "skipped": True}

    store.set_state("last_sync_at", str(time.time()))
    report["tables"] = total_tables
    report["columns"] = total_columns
    report["duration_seconds"] = round(time.time() - started, 1)
    report["summary"] = store.snapshot_summary()
    emit("done", report)
    return report


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = [a for a in argv if not a.startswith("--")]
    embed = "--no-embed" not in argv
    full = "--full" in argv

    def show(stage: str, info: dict) -> None:
        print(f"[{stage}] {info}")

    report = run_sync(catalogs=args or None, embed=embed, full_lineage=full, progress=show)
    print()
    print(f"tables={report['tables']} columns={report['columns']} "
          f"edges={report.get('lineage', {}).get('edges_seen', 0)} "
          f"embedded={report.get('index', {}).get('embedded', 0)} "
          f"in {report['duration_seconds']}s")
    for err in report["errors"]:
        print(f"  error: {err}")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
