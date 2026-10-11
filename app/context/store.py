"""SQLite persistence for the metadata snapshot, the embedding index and
conversation state.

One file, `data/contextengine.db`, holds everything: the Unity Catalog
snapshot (tables, columns, lineage edges), the vector and FTS indexes built
over it, and the chat sessions with their approved artifacts. Keeping the
snapshot local is what makes lineage traversal and retrieval free - the
warehouse is only touched during an explicit sync.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Sequence

from app.config import get_settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tables_meta (
    catalog      TEXT NOT NULL,
    schema_name  TEXT NOT NULL,
    table_name   TEXT NOT NULL,
    table_type   TEXT,
    owner        TEXT,
    comment      TEXT,
    synced_at    REAL NOT NULL,
    PRIMARY KEY (catalog, schema_name, table_name)
);
CREATE INDEX IF NOT EXISTS idx_tables_schema ON tables_meta (catalog, schema_name);

CREATE TABLE IF NOT EXISTS columns_meta (
    catalog      TEXT NOT NULL,
    schema_name  TEXT NOT NULL,
    table_name   TEXT NOT NULL,
    column_name  TEXT NOT NULL,
    data_type    TEXT,
    comment      TEXT,
    ordinal      INTEGER,
    nullable     TEXT,
    PRIMARY KEY (catalog, schema_name, table_name, column_name)
);
CREATE INDEX IF NOT EXISTS idx_columns_table ON columns_meta (catalog, schema_name, table_name);
CREATE INDEX IF NOT EXISTS idx_columns_name ON columns_meta (column_name);

CREATE TABLE IF NOT EXISTS lineage_edges (
    source_id    TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    entity_type  TEXT NOT NULL DEFAULT '',
    entity_id    TEXT NOT NULL DEFAULT '',
    occurrences  INTEGER NOT NULL DEFAULT 1,
    last_event   TEXT,
    PRIMARY KEY (source_id, target_id, entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON lineage_edges (source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON lineage_edges (target_id);

CREATE TABLE IF NOT EXISTS docs (
    doc_id       TEXT PRIMARY KEY,
    doc_type     TEXT NOT NULL,
    table_id     TEXT NOT NULL,
    column_name  TEXT,
    body         TEXT NOT NULL,
    body_hash    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_table ON docs (table_id);

CREATE TABLE IF NOT EXISTS embeddings (
    doc_id       TEXT PRIMARY KEY,
    model        TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    body_hash    TEXT NOT NULL,
    vector       BLOB NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES docs (doc_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sync_state (
    key          TEXT PRIMARY KEY,
    value        TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    title        TEXT,
    stage        TEXT NOT NULL,
    state_json   TEXT NOT NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    role         TEXT NOT NULL,
    stage        TEXT,
    content      TEXT NOT NULL,
    meta_json    TEXT,
    created_at   REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    version      INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    approved     INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts (session_id, kind, version);

CREATE TABLE IF NOT EXISTS usage_log (
    id            TEXT PRIMARY KEY,
    session_id    TEXT,
    stage         TEXT,
    model         TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cache_read_tokens  INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_log (session_id);

CREATE TABLE IF NOT EXISTS deployments (
    id           TEXT PRIMARY KEY,
    session_id   TEXT,
    project_name TEXT NOT NULL,
    job_id       INTEGER,
    job_url      TEXT,
    last_run_id  INTEGER,
    status       TEXT,
    detail_json  TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    doc_id UNINDEXED,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def table_id(catalog: str, schema: str, table: str) -> str:
    return f"{catalog}.{schema}.{table}"


@dataclass
class TableRecord:
    catalog: str
    schema_name: str
    table_name: str
    table_type: str | None
    owner: str | None
    comment: str | None

    @property
    def id(self) -> str:
        return table_id(self.catalog, self.schema_name, self.table_name)


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.fts_enabled = False
        self.vec_enabled = False
        self._initialized = False

    # ------------------------------------------------------------------
    # connections
    # ------------------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            self._load_vec_extension(conn)
            self._local.conn = conn
        return conn

    def _load_vec_extension(self, conn: sqlite3.Connection) -> None:
        """sqlite-vec is a loadable extension, so it has to be attached to
        every connection - and connections here are per-thread. Any failure
        (package missing, extension loading disabled in this Python build)
        leaves vec_enabled False and retrieval uses the numpy scan."""
        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self.vec_enabled = True
        except Exception:  # noqa: BLE001 - optional accelerator only
            self.vec_enabled = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._write_lock:
            self.conn.executescript(SCHEMA)
            try:
                self.conn.executescript(FTS_SCHEMA)
                self.fts_enabled = True
            except sqlite3.OperationalError:
                # FTS5 is compiled out of some Python SQLite builds. Lexical
                # search then falls back to LIKE matching in retrieval.py.
                self.fts_enabled = False
            self.conn.commit()
            self._initialized = True

    def _write(self, fn, *args, **kwargs):
        with self._write_lock:
            result = fn(*args, **kwargs)
            self.conn.commit()
            return result

    def query(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    # ------------------------------------------------------------------
    # snapshot writes
    # ------------------------------------------------------------------

    def replace_catalog_tables(self, catalog: str, rows: Iterable[dict]) -> int:
        """Swap in a fresh table list for one catalog. Tables that vanished
        upstream are removed here, and their columns/docs/embeddings cascade
        away with them so retrieval can never surface a dropped table."""
        now = time.time()
        payload = [
            (
                catalog,
                r["schema_name"],
                r["table_name"],
                r.get("table_type"),
                r.get("owner"),
                r.get("comment"),
                now,
            )
            for r in rows
        ]

        def op():
            self.conn.execute("DELETE FROM tables_meta WHERE catalog = ?", (catalog,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO tables_meta "
                "(catalog, schema_name, table_name, table_type, owner, comment, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            # Columns and docs are keyed by table, so drop the orphans.
            self.conn.execute(
                "DELETE FROM columns_meta WHERE catalog = ? AND (schema_name, table_name) NOT IN "
                "(SELECT schema_name, table_name FROM tables_meta WHERE catalog = ?)",
                (catalog, catalog),
            )
            self.conn.execute(
                "DELETE FROM docs WHERE table_id LIKE ? AND table_id NOT IN "
                "(SELECT catalog || '.' || schema_name || '.' || table_name FROM tables_meta)",
                (f"{catalog}.%",),
            )
            return len(payload)

        return self._write(op)

    def replace_catalog_columns(self, catalog: str, rows: Iterable[dict]) -> int:
        payload = [
            (
                catalog,
                r["schema_name"],
                r["table_name"],
                r["column_name"],
                r.get("data_type"),
                r.get("comment"),
                r.get("ordinal"),
                r.get("nullable"),
            )
            for r in rows
        ]

        def op():
            self.conn.execute("DELETE FROM columns_meta WHERE catalog = ?", (catalog,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO columns_meta "
                "(catalog, schema_name, table_name, column_name, data_type, comment, ordinal, nullable) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            return len(payload)

        return self._write(op)

    def upsert_lineage_edges(self, edges: Iterable[dict]) -> int:
        """Merge lineage rows in. Re-running a sync over an overlapping window
        bumps occurrence counts and event times rather than duplicating."""
        payload = [
            (
                e["source_id"],
                e["target_id"],
                e.get("entity_type") or "",
                str(e.get("entity_id") or ""),
                e.get("occurrences", 1),
                e.get("last_event"),
            )
            for e in edges
        ]

        def op():
            self.conn.executemany(
                """
                INSERT INTO lineage_edges (source_id, target_id, entity_type, entity_id, occurrences, last_event)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_id, target_id, entity_type, entity_id) DO UPDATE SET
                    occurrences = lineage_edges.occurrences + excluded.occurrences,
                    last_event = MAX(COALESCE(excluded.last_event, ''), COALESCE(lineage_edges.last_event, ''))
                """,
                payload,
            )
            return len(payload)

        return self._write(op)

    def clear_lineage(self) -> None:
        self._write(self.conn.execute, "DELETE FROM lineage_edges")

    def prune_lineage_to_known_tables(self) -> int:
        """Drop edges whose endpoints are not (or are no longer) real tables
        in the snapshot - the equivalent of the POC's `_is_live` filter,
        applied once at sync time instead of on every read."""

        def op():
            cur = self.conn.execute(
                """
                DELETE FROM lineage_edges
                WHERE source_id NOT IN (SELECT catalog || '.' || schema_name || '.' || table_name FROM tables_meta)
                   OR target_id NOT IN (SELECT catalog || '.' || schema_name || '.' || table_name FROM tables_meta)
                """
            )
            return cur.rowcount

        return self._write(op)

    # ------------------------------------------------------------------
    # sync bookkeeping
    # ------------------------------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM sync_state WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self._write(
            self.conn.execute,
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            (key, value),
        )

    def snapshot_summary(self) -> dict:
        try:
            tables = self.query_one("SELECT COUNT(*) AS n FROM tables_meta")["n"]
            columns = self.query_one("SELECT COUNT(*) AS n FROM columns_meta")["n"]
            edges = self.query_one("SELECT COUNT(*) AS n FROM lineage_edges")["n"]
            embedded = self.query_one("SELECT COUNT(*) AS n FROM embeddings")["n"]
            docs = self.query_one("SELECT COUNT(*) AS n FROM docs")["n"]
            catalogs = [r["catalog"] for r in self.query("SELECT DISTINCT catalog FROM tables_meta ORDER BY catalog")]
        except sqlite3.OperationalError:
            return {"initialized": False}
        commented = self.query_one(
            "SELECT COUNT(*) AS n FROM columns_meta WHERE comment IS NOT NULL AND TRIM(comment) <> ''"
        )["n"]
        return {
            "initialized": True,
            "catalogs": catalogs,
            "tables": tables,
            "columns": columns,
            "lineage_edges": edges,
            "docs": docs,
            "embedded_docs": embedded,
            "column_comment_coverage": round(commented / columns, 3) if columns else 0.0,
            "last_sync": self.get_state("last_sync_at"),
        }

    # ------------------------------------------------------------------
    # snapshot reads
    # ------------------------------------------------------------------

    def get_table(self, tid: str) -> dict | None:
        row = self.query_one(
            "SELECT * FROM tables_meta WHERE catalog || '.' || schema_name || '.' || table_name = ?",
            (tid,),
        )
        return dict(row) if row else None

    def get_columns(self, tid: str) -> list[dict]:
        parts = tid.split(".")
        if len(parts) != 3:
            return []
        rows = self.query(
            "SELECT column_name, data_type, comment, ordinal FROM columns_meta "
            "WHERE catalog = ? AND schema_name = ? AND table_name = ? ORDER BY ordinal",
            parts,
        )
        return [dict(r) for r in rows]

    def list_catalogs(self) -> list[str]:
        return [r["catalog"] for r in self.query("SELECT DISTINCT catalog FROM tables_meta ORDER BY catalog")]

    def list_schemas(self, catalog: str) -> list[dict]:
        rows = self.query(
            "SELECT schema_name, COUNT(*) AS table_count FROM tables_meta WHERE catalog = ? "
            "GROUP BY schema_name ORDER BY schema_name",
            (catalog,),
        )
        return [dict(r) for r in rows]

    def list_tables(self, catalog: str, schema: str | None = None) -> list[dict]:
        if schema:
            rows = self.query(
                "SELECT * FROM tables_meta WHERE catalog = ? AND schema_name = ? ORDER BY table_name",
                (catalog, schema),
            )
        else:
            rows = self.query("SELECT * FROM tables_meta WHERE catalog = ? ORDER BY schema_name, table_name", (catalog,))
        return [dict(r) for r in rows]

    def edges_for(self, ids: Sequence[str]) -> list[dict]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self.query(
            f"SELECT * FROM lineage_edges WHERE source_id IN ({marks}) OR target_id IN ({marks})",
            list(ids) + list(ids),
        )
        return [dict(r) for r in rows]

    def neighbors(self, tid: str) -> tuple[list[str], list[str]]:
        up = [r["source_id"] for r in self.query("SELECT DISTINCT source_id FROM lineage_edges WHERE target_id = ?", (tid,))]
        down = [r["target_id"] for r in self.query("SELECT DISTINCT target_id FROM lineage_edges WHERE source_id = ?", (tid,))]
        return up, down

    # ------------------------------------------------------------------
    # documents / index
    # ------------------------------------------------------------------

    def upsert_docs(self, docs: Sequence[dict]) -> None:
        def op():
            self.conn.executemany(
                "INSERT OR REPLACE INTO docs (doc_id, doc_type, table_id, column_name, body, body_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(d["doc_id"], d["doc_type"], d["table_id"], d.get("column_name"), d["body"], d["body_hash"]) for d in docs],
            )
            if self.fts_enabled:
                self.conn.executemany("DELETE FROM docs_fts WHERE doc_id = ?", [(d["doc_id"],) for d in docs])
                self.conn.executemany(
                    "INSERT INTO docs_fts (doc_id, body) VALUES (?, ?)",
                    [(d["doc_id"], d["body"]) for d in docs],
                )

        self._write(op)

    def stale_docs(self, model: str, dim: int) -> list[dict]:
        """Docs with no embedding, or whose body changed since it was made."""
        rows = self.query(
            """
            SELECT d.doc_id, d.body, d.body_hash
            FROM docs d
            LEFT JOIN embeddings e ON e.doc_id = d.doc_id
            WHERE e.doc_id IS NULL OR e.body_hash <> d.body_hash OR e.model <> ? OR e.dim <> ?
            """,
            (model, dim),
        )
        return [dict(r) for r in rows]

    def upsert_embeddings(self, records: Sequence[tuple[str, str, int, str, bytes]]) -> None:
        self._write(
            self.conn.executemany,
            "INSERT OR REPLACE INTO embeddings (doc_id, model, dim, body_hash, vector) VALUES (?, ?, ?, ?, ?)",
            records,
        )

    def all_embeddings(self, model: str, dim: int) -> tuple[list[str], list[bytes]]:
        rows = self.query(
            "SELECT doc_id, vector FROM embeddings WHERE model = ? AND dim = ? ORDER BY doc_id",
            (model, dim),
        )
        return [r["doc_id"] for r in rows], [r["vector"] for r in rows]

    def docs_by_id(self, doc_ids: Sequence[str]) -> dict[str, dict]:
        if not doc_ids:
            return {}
        marks = ",".join("?" * len(doc_ids))
        rows = self.query(f"SELECT * FROM docs WHERE doc_id IN ({marks})", list(doc_ids))
        return {r["doc_id"]: dict(r) for r in rows}

    def index_version(self) -> str:
        """Cheap fingerprint of the embedding table, used to invalidate the
        in-memory matrix without tracking every write path."""
        row = self.query_one("SELECT COUNT(*) AS n, COALESCE(MAX(body_hash), '') AS h FROM embeddings")
        return f"{row['n']}:{row['h']}"

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------

    def create_session(self, title: str, stage: str, state: dict) -> str:
        sid = uuid.uuid4().hex
        now = time.time()
        self._write(
            self.conn.execute,
            "INSERT INTO sessions (id, title, stage, state_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, title, stage, json.dumps(state), now, now),
        )
        return sid

    def get_session(self, sid: str) -> dict | None:
        row = self.query_one("SELECT * FROM sessions WHERE id = ?", (sid,))
        if not row:
            return None
        data = dict(row)
        data["state"] = json.loads(data.pop("state_json"))
        return data

    def update_session(self, sid: str, stage: str | None = None, state: dict | None = None, title: str | None = None) -> None:
        sets, params = ["updated_at = ?"], [time.time()]
        if stage is not None:
            sets.append("stage = ?")
            params.append(stage)
        if state is not None:
            sets.append("state_json = ?")
            params.append(json.dumps(state))
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        params.append(sid)
        self._write(self.conn.execute, f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params)

    def list_sessions(self, limit: int = 50) -> list[dict]:
        rows = self.query(
            "SELECT id, title, stage, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def delete_session(self, sid: str) -> None:
        self._write(self.conn.execute, "DELETE FROM sessions WHERE id = ?", (sid,))

    def add_message(self, sid: str, role: str, content: str, stage: str | None = None, meta: dict | None = None) -> dict:
        mid = uuid.uuid4().hex
        now = time.time()
        self._write(
            self.conn.execute,
            "INSERT INTO messages (id, session_id, role, stage, content, meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mid, sid, role, stage, content, json.dumps(meta or {}), now),
        )
        return {"id": mid, "session_id": sid, "role": role, "stage": stage, "content": content, "meta": meta or {}, "created_at": now}

    def get_messages(self, sid: str) -> list[dict]:
        rows = self.query("SELECT * FROM messages WHERE session_id = ? ORDER BY created_at", (sid,))
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d.pop("meta_json") or "{}")
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # artifacts
    # ------------------------------------------------------------------

    def save_artifact(self, sid: str, kind: str, payload: dict, approved: bool = False) -> dict:
        row = self.query_one(
            "SELECT COALESCE(MAX(version), 0) AS v FROM artifacts WHERE session_id = ? AND kind = ?",
            (sid, kind),
        )
        version = (row["v"] if row else 0) + 1
        aid = uuid.uuid4().hex
        now = time.time()
        self._write(
            self.conn.execute,
            "INSERT INTO artifacts (id, session_id, kind, version, payload_json, approved, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (aid, sid, kind, version, json.dumps(payload), int(approved), now),
        )
        return {"id": aid, "kind": kind, "version": version, "payload": payload, "approved": approved, "created_at": now}

    def latest_artifact(self, sid: str, kind: str) -> dict | None:
        row = self.query_one(
            "SELECT * FROM artifacts WHERE session_id = ? AND kind = ? ORDER BY version DESC LIMIT 1",
            (sid, kind),
        )
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        d["approved"] = bool(d["approved"])
        return d

    def approve_artifact(self, artifact_id: str) -> None:
        self._write(self.conn.execute, "UPDATE artifacts SET approved = 1 WHERE id = ?", (artifact_id,))

    def artifact_history(self, sid: str, kind: str) -> list[dict]:
        rows = self.query(
            "SELECT id, kind, version, approved, created_at FROM artifacts WHERE session_id = ? AND kind = ? ORDER BY version",
            (sid, kind),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # usage / deployments
    # ------------------------------------------------------------------

    def log_usage(self, sid: str | None, stage: str, model: str, usage: dict, cost: float) -> None:
        self._write(
            self.conn.execute,
            "INSERT INTO usage_log (id, session_id, stage, model, input_tokens, output_tokens, "
            "cache_write_tokens, cache_read_tokens, cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                sid,
                stage,
                model,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("cache_write_tokens", 0),
                usage.get("cache_read_tokens", 0),
                cost,
                time.time(),
            ),
        )

    def session_usage(self, sid: str) -> dict:
        row = self.query_one(
            "SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd, COUNT(*) AS calls FROM usage_log WHERE session_id = ?",
            (sid,),
        )
        data = dict(row) if row else {}
        data["total_tokens"] = data.get("input_tokens", 0) + data.get("output_tokens", 0) + data.get("cache_read_tokens", 0)
        data["cost_usd"] = round(data.get("cost_usd", 0.0), 6)
        return data

    def save_deployment(self, sid: str | None, project_name: str, **fields: Any) -> dict:
        row = self.query_one("SELECT * FROM deployments WHERE project_name = ?", (project_name,))
        now = time.time()
        if row:
            sets, params = ["updated_at = ?"], [now]
            for key, value in fields.items():
                sets.append(f"{key} = ?")
                params.append(json.dumps(value) if key == "detail_json" and not isinstance(value, str) else value)
            params.append(row["id"])
            self._write(self.conn.execute, f"UPDATE deployments SET {', '.join(sets)} WHERE id = ?", params)
            did = row["id"]
        else:
            did = uuid.uuid4().hex
            self._write(
                self.conn.execute,
                "INSERT INTO deployments (id, session_id, project_name, job_id, job_url, last_run_id, status, detail_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    did,
                    sid,
                    project_name,
                    fields.get("job_id"),
                    fields.get("job_url"),
                    fields.get("last_run_id"),
                    fields.get("status"),
                    fields.get("detail_json"),
                    now,
                    now,
                ),
            )
        return dict(self.query_one("SELECT * FROM deployments WHERE id = ?", (did,)))

    def get_deployment(self, project_name: str) -> dict | None:
        row = self.query_one("SELECT * FROM deployments WHERE project_name = ?", (project_name,))
        return dict(row) if row else None


@lru_cache(maxsize=1)
def get_store() -> Store:
    store = Store(str(get_settings().db_path))
    store.initialize()
    return store
