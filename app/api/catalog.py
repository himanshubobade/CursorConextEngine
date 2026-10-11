"""Catalog snapshot, search and lineage.

Every read here hits the local SQLite snapshot, not the warehouse - the
only endpoint that talks to Databricks is /catalog/sync.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.context import lineage_graph, sync
from app.context.embeddings import VectorIndex
from app.context.framework_spec import describe as describe_framework
from app.context.retrieval import search_tables
from app.context.store import get_store

router = APIRouter(prefix="/api/catalog", tags=["catalog"])

_SENTINEL = object()


class SyncRequest(BaseModel):
    catalogs: list[str] | None = None
    embed: bool = True
    full_lineage: bool = False


@router.get("/summary")
def summary() -> dict:
    return {
        "snapshot": get_store().snapshot_summary(),
        "vector_index_size": VectorIndex.get().size,
        "framework": describe_framework(),
    }


@router.post("/sync")
async def run_sync(body: SyncRequest) -> StreamingResponse:
    """Streams progress while the snapshot refreshes.

    The sync itself is blocking HTTP against Databricks, so it runs on a
    worker thread and pushes events through a queue rather than holding the
    event loop for the duration.
    """
    events: queue.Queue = queue.Queue()

    def emit(stage: str, info: dict) -> None:
        events.put({"type": "progress", "stage": stage, **info})

    def worker() -> None:
        try:
            report = sync.run_sync(
                catalogs=body.catalogs,
                embed=body.embed,
                full_lineage=body.full_lineage,
                progress=emit,
            )
            events.put({"type": "complete", "report": report})
        except Exception as exc:  # noqa: BLE001 - reported to the browser
            events.put({"type": "error", "message": str(exc)})
        finally:
            events.put(_SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    async def stream() -> AsyncIterator[str]:
        while True:
            item = await asyncio.to_thread(events.get)
            if item is _SENTINEL:
                break
            yield f"data: {json.dumps(item, default=str)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/catalogs")
def catalogs() -> dict:
    return {"catalogs": get_store().list_catalogs()}


@router.get("/search")
def search(q: str = Query(min_length=2), top_k: int = 10, rerank: bool = False) -> dict:
    """Ad-hoc table search for the review panel's "add a table" box.

    Reranking is off by default here because this runs on every search the
    analyst types, unlike the once-per-request discovery stage.
    """
    return search_tables(q, top_k=top_k, use_rerank=rerank)


@router.get("/{catalog}/schemas")
def schemas(catalog: str) -> dict:
    return lineage_graph.catalog_overview(get_store(), catalog)


@router.get("/{catalog}/schema/{schema}/tables")
def tables(catalog: str, schema: str) -> dict:
    return lineage_graph.schema_tables(get_store(), catalog, schema)


@router.get("/table/{table_id}")
def table_detail(table_id: str) -> dict:
    store = get_store()
    meta = store.get_table(table_id)
    if meta is None:
        raise HTTPException(404, f"{table_id} is not in the snapshot")
    up, down = store.neighbors(table_id)
    return {
        "table": meta,
        "columns": store.get_columns(table_id),
        "upstream": up,
        "downstream": down,
    }


@router.get("/table/{table_id}/lineage")
def table_lineage(table_id: str, hops: int = 1) -> dict:
    store = get_store()
    if store.get_table(table_id) is None:
        raise HTTPException(404, f"{table_id} is not in the snapshot")
    return lineage_graph.table_lineage(store, table_id, hops=hops)


@router.post("/projection")
def projection(payload: dict[str, Any]) -> dict:
    """Projected lineage for an arbitrary source/sink pair, used when the
    analyst edits the plan's tables before regenerating."""
    return lineage_graph.projected_graph(
        None,
        payload.get("source_tables") or [],
        payload.get("sink_table"),
        payload.get("project_name") or "project",
    )
