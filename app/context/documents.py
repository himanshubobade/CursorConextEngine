"""Turns snapshot rows into the text documents that get embedded and indexed.

Two document types, because they answer different questions. A table doc
matches "where does revenue live"; a column doc matches "net_revenue_amt"
and then rolls up to its table. Lineage is deliberately *not* part of either
body: it changes far more often than schemas do, and folding it into the
text would re-embed half the catalog on every sync. Graph position is
applied at query time as a separate prior instead.
"""
from __future__ import annotations

import hashlib
import re

from app.context.store import Store, table_id

MAX_COLUMNS_IN_TABLE_DOC = 120


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def humanize_identifier(name: str) -> str:
    """`net_revenue_amt` -> `net revenue amt`, `NetRevenue` -> `Net Revenue`.

    Gives both the embedder and the FTS tokenizer the individual words that
    a plain-English question is actually made of.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    spaced = spaced.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", spaced).strip()


def table_doc_body(table: dict, columns: list[dict]) -> str:
    tid = table_id(table["catalog"], table["schema_name"], table["table_name"])
    lines = [
        f"Table: {tid}",
        f"Name: {table['table_name']} ({humanize_identifier(table['table_name'])})",
        f"Schema: {table['schema_name']} ({humanize_identifier(table['schema_name'])})",
        f"Catalog: {table['catalog']}",
    ]
    if table.get("table_type"):
        lines.append(f"Type: {table['table_type']}")
    if table.get("owner"):
        lines.append(f"Owner: {table['owner']}")
    if (table.get("comment") or "").strip():
        lines.append(f"Description: {table['comment'].strip()}")

    if columns:
        shown = columns[:MAX_COLUMNS_IN_TABLE_DOC]
        lines.append("Columns:")
        for col in shown:
            piece = f"  - {col['column_name']} ({col.get('data_type') or 'unknown'})"
            comment = (col.get("comment") or "").strip()
            if comment:
                piece += f": {comment}"
            lines.append(piece)
        if len(columns) > len(shown):
            lines.append(f"  ... and {len(columns) - len(shown)} more columns")
        # A flat word bag of the column names so a question phrased in prose
        # ("monthly revenue by product") can match snake_case identifiers.
        lines.append("Column terms: " + ", ".join(humanize_identifier(c["column_name"]) for c in shown))

    return "\n".join(lines)


def column_doc_body(table: dict, column: dict) -> str:
    tid = table_id(table["catalog"], table["schema_name"], table["table_name"])
    lines = [
        f"Column: {tid}.{column['column_name']}",
        f"Name: {column['column_name']} ({humanize_identifier(column['column_name'])})",
        f"Data type: {column.get('data_type') or 'unknown'}",
        f"In table: {tid}",
    ]
    comment = (column.get("comment") or "").strip()
    if comment:
        lines.append(f"Description: {comment}")
    table_comment = (table.get("comment") or "").strip()
    if table_comment:
        lines.append(f"Table description: {table_comment}")
    return "\n".join(lines)


def build_documents(store: Store, catalog: str | None = None) -> list[dict]:
    """Build every table and column doc for one catalog (or all of them)."""
    if catalog:
        tables = store.query("SELECT * FROM tables_meta WHERE catalog = ?", (catalog,))
        columns = store.query(
            "SELECT * FROM columns_meta WHERE catalog = ? ORDER BY schema_name, table_name, ordinal", (catalog,)
        )
    else:
        tables = store.query("SELECT * FROM tables_meta")
        columns = store.query("SELECT * FROM columns_meta ORDER BY catalog, schema_name, table_name, ordinal")

    by_table: dict[str, list[dict]] = {}
    for row in columns:
        tid = table_id(row["catalog"], row["schema_name"], row["table_name"])
        by_table.setdefault(tid, []).append(dict(row))

    docs: list[dict] = []
    for row in tables:
        table = dict(row)
        tid = table_id(table["catalog"], table["schema_name"], table["table_name"])
        cols = by_table.get(tid, [])

        body = table_doc_body(table, cols)
        docs.append(
            {
                "doc_id": f"table::{tid}",
                "doc_type": "table",
                "table_id": tid,
                "column_name": None,
                "body": body,
                "body_hash": _hash(body),
            }
        )

        for col in cols:
            col_body = column_doc_body(table, col)
            docs.append(
                {
                    "doc_id": f"column::{tid}.{col['column_name']}",
                    "doc_type": "column",
                    "table_id": tid,
                    "column_name": col["column_name"],
                    "body": col_body,
                    "body_hash": _hash(col_body),
                }
            )

    return docs
