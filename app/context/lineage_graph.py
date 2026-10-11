"""Lineage traversal over the local snapshot.

Everything here reads `lineage_edges` in SQLite, so a multi-hop walk costs
microseconds and can be done on every retrieval query. The same primitives
build the projected graph shown at plan review: the proposed table is just
another node, marked so the UI can draw it differently.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Iterable, Literal

from app.context.store import Store, get_store, table_id

Direction = Literal["upstream", "downstream", "both"]


@dataclass
class Reach:
    """How a table was reached from the seed set."""

    distance: int
    direction: str
    seeds: set[str] = field(default_factory=set)


def neighborhood(
    store: Store,
    seeds: Iterable[str],
    hops: int = 2,
    direction: Direction = "both",
) -> dict[str, Reach]:
    """Breadth-first walk out from `seeds`, recording the shortest hop count
    and which seeds each table was reachable from.

    The seed set itself is included at distance 0. Multi-seed reachability is
    what lets retrieval boost a table that sits between two other candidates.
    """
    seeds = [s for s in seeds if s]
    reach: dict[str, Reach] = {s: Reach(0, "seed", {s}) for s in seeds}
    queue: deque[tuple[str, int, str]] = deque((s, 0, "seed") for s in seeds)

    while queue:
        node, dist, path_dir = queue.popleft()
        if dist >= hops:
            continue
        up, down = store.neighbors(node)
        steps: list[tuple[str, str]] = []
        if direction in ("upstream", "both"):
            steps += [(n, "upstream") for n in up]
        if direction in ("downstream", "both"):
            steps += [(n, "downstream") for n in down]

        for neighbor, step_dir in steps:
            origin = reach[node].seeds
            existing = reach.get(neighbor)
            if existing is None:
                reach[neighbor] = Reach(dist + 1, step_dir if path_dir == "seed" else "mixed", set(origin))
                queue.append((neighbor, dist + 1, step_dir if path_dir == "seed" else "mixed"))
            else:
                existing.seeds |= origin
                if dist + 1 < existing.distance:
                    existing.distance = dist + 1
                    queue.append((neighbor, dist + 1, existing.direction))

    return reach


def edges_between(store: Store, ids: Iterable[str]) -> list[dict]:
    """Deduplicated source -> target edges among a node set, with the
    transformation assets responsible collapsed onto each edge."""
    id_set = set(ids)
    if not id_set:
        return []
    grouped: dict[tuple[str, str], dict] = {}
    for row in store.edges_for(list(id_set)):
        if row["source_id"] not in id_set or row["target_id"] not in id_set:
            continue
        key = (row["source_id"], row["target_id"])
        edge = grouped.setdefault(
            key,
            {"source": row["source_id"], "target": row["target_id"], "occurrences": 0, "assets": []},
        )
        edge["occurrences"] += row["occurrences"] or 0
        if row["entity_type"]:
            asset = {"type": row["entity_type"], "id": row["entity_id"], "last_event": row["last_event"]}
            if not any(a["type"] == asset["type"] and a["id"] == asset["id"] for a in edge["assets"]):
                edge["assets"].append(asset)
    return list(grouped.values())


def node_payload(store: Store, tid: str, extra: dict | None = None) -> dict:
    meta = store.get_table(tid) or {}
    parts = tid.split(".")
    payload = {
        "id": tid,
        "type": "table",
        "label": parts[-1] if parts else tid,
        "catalog": meta.get("catalog") or (parts[0] if len(parts) == 3 else None),
        "schema": meta.get("schema_name") or (parts[1] if len(parts) == 3 else None),
        "table_type": meta.get("table_type"),
        "owner": meta.get("owner"),
        "comment": meta.get("comment"),
        "known": bool(meta),
    }
    if extra:
        payload.update(extra)
    return payload


def subgraph(store: Store, ids: Iterable[str], reach: dict[str, Reach] | None = None) -> dict:
    ids = list(dict.fromkeys(ids))
    nodes = []
    for tid in ids:
        extra = {}
        if reach and tid in reach:
            extra = {"distance": reach[tid].distance, "direction": reach[tid].direction}
        nodes.append(node_payload(store, tid, extra))
    return {"nodes": nodes, "edges": edges_between(store, ids)}


# ----------------------------------------------------------------------
# catalog / schema / table views (the uc-lineage-web drill-down, served
# from the local snapshot instead of the warehouse)
# ----------------------------------------------------------------------


def catalog_overview(store: Store, catalog: str) -> dict:
    schemas = store.list_schemas(catalog)
    nodes = [
        {
            "id": s["schema_name"],
            "type": "schema",
            "label": s["schema_name"],
            "table_count": s["table_count"],
            "external_catalogs": [],
        }
        for s in schemas
    ]
    known = {n["id"] for n in nodes}

    pair_weights: dict[tuple[str, str], int] = defaultdict(int)
    external: dict[str, set[str]] = defaultdict(set)

    for row in store.query(
        "SELECT source_id, target_id, SUM(occurrences) AS occurrences FROM lineage_edges GROUP BY source_id, target_id"
    ):
        src = row["source_id"].split(".")
        tgt = row["target_id"].split(".")
        if len(src) != 3 or len(tgt) != 3:
            continue
        src_local, tgt_local = src[0] == catalog, tgt[0] == catalog
        if not (src_local or tgt_local):
            continue
        if src_local and tgt_local:
            if src[1] != tgt[1] and src[1] in known and tgt[1] in known:
                pair_weights[(src[1], tgt[1])] += int(row["occurrences"] or 1)
        elif src_local:
            external[src[1]].add(tgt[0])
        else:
            external[tgt[1]].add(src[0])

    for node in nodes:
        node["external_catalogs"] = sorted(external.get(node["id"], []))

    edges = [{"source": s, "target": t, "weight": w} for (s, t), w in pair_weights.items()]
    return {"catalog": catalog, "nodes": nodes, "edges": edges}


def schema_tables(store: Store, catalog: str, schema: str) -> dict:
    """Tables in one schema plus stub nodes for anything they connect to
    elsewhere, so the graph shows the relationship without loading the
    neighbouring schema in full."""
    local = store.list_tables(catalog, schema)
    local_ids = {table_id(catalog, schema, t["table_name"]) for t in local}
    nodes = [node_payload(store, tid) for tid in sorted(local_ids)]

    stubs: dict[str, dict] = {}
    out_edges = []
    for row in store.edges_for(list(local_ids)):
        src, tgt = row["source_id"], row["target_id"]
        src_local, tgt_local = src in local_ids, tgt in local_ids
        if not (src_local or tgt_local):
            continue
        for other, is_local in ((src, src_local), (tgt, tgt_local)):
            if not is_local and other not in stubs:
                parts = other.split(".")
                stubs[other] = node_payload(
                    store,
                    other,
                    {
                        "type": "table_stub",
                        "schema": parts[1] if len(parts) == 3 else None,
                        "cross_catalog": len(parts) == 3 and parts[0] != catalog,
                    },
                )
        out_edges.append({"source": src, "target": tgt, "assets": [], "occurrences": row["occurrences"]})

    merged: dict[tuple[str, str], dict] = {}
    for edge in out_edges:
        key = (edge["source"], edge["target"])
        if key in merged:
            merged[key]["occurrences"] += edge["occurrences"] or 0
        else:
            merged[key] = edge

    nodes.extend(stubs.values())
    return {"catalog": catalog, "schema": schema, "nodes": nodes, "edges": list(merged.values())}


def table_lineage(store: Store, tid: str, hops: int = 1) -> dict:
    """Focal table with its upstream and downstream neighbours, shaped for
    the lineage panel."""
    up_reach = neighborhood(store, [tid], hops=hops, direction="upstream")
    down_reach = neighborhood(store, [tid], hops=hops, direction="downstream")

    upstream = [node_payload(store, n, {"distance": r.distance}) for n, r in up_reach.items() if n != tid]
    downstream = [node_payload(store, n, {"distance": r.distance}) for n, r in down_reach.items() if n != tid]

    all_ids = [tid] + [n["id"] for n in upstream] + [n["id"] for n in downstream]
    return {
        "table": {
            **node_payload(store, tid),
            "columns": store.get_columns(tid),
        },
        "upstream": sorted(upstream, key=lambda n: (n["distance"], n["id"])),
        "downstream": sorted(downstream, key=lambda n: (n["distance"], n["id"])),
        "edges": edges_between(store, all_ids),
    }


# ----------------------------------------------------------------------
# projected lineage
# ----------------------------------------------------------------------


def projected_graph(
    store: Store | None,
    source_ids: list[str],
    sink_id: str | None,
    project_name: str,
    context_hops: int = 1,
) -> dict:
    """The existing graph around the chosen sources, plus the table this
    project would create and the edges it would add.

    Proposed nodes and edges are flagged rather than inserted, so nothing
    here mutates the snapshot - the preview and the real graph stay separate
    until the job actually runs and Unity Catalog records the lineage itself.
    """
    store = store or get_store()
    context = neighborhood(store, source_ids, hops=context_hops)
    ids = list(dict.fromkeys(list(context.keys()) + source_ids))

    graph = subgraph(store, ids, context)
    for node in graph["nodes"]:
        node["role"] = "source" if node["id"] in source_ids else "context"
    for edge in graph["edges"]:
        edge["status"] = "existing"

    if sink_id:
        existing = store.get_table(sink_id)
        parts = sink_id.split(".")
        graph["nodes"].append(
            {
                "id": sink_id,
                "type": "table",
                "label": parts[-1],
                "catalog": parts[0] if len(parts) == 3 else None,
                "schema": parts[1] if len(parts) == 3 else None,
                "known": bool(existing),
                "role": "sink",
                "status": "proposed" if not existing else "proposed_overwrite",
                "comment": (existing or {}).get("comment"),
            }
        )
        for src in source_ids:
            graph["edges"].append(
                {
                    "source": src,
                    "target": sink_id,
                    "status": "proposed",
                    "assets": [{"type": "JOB", "id": f"ingestion_{project_name}", "proposed": True}],
                    "occurrences": 0,
                }
            )

    graph["project_name"] = project_name
    graph["sink"] = sink_id
    graph["sources"] = source_ids
    return graph
