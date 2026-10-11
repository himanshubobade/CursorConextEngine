"""End-to-end check of the pipeline with no external services.

Seeds a small synthetic catalog into a throwaway database, replaces the
embedding and LLM calls with deterministic stand-ins, then drives the state
machine from a plain-English request through to approved code. Everything
else - retrieval fusion, lineage scoring, the projected graph, the framework
spec card, and all five validators - runs for real.

    python scripts/selftest.py              # pipeline only
    python scripts/selftest.py --publish    # also exercise the git publish path
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from app.config import get_settings  # noqa: E402

# Point the store at a scratch database before anything opens it.
# ignore_cleanup_errors: SQLite keeps the database file open until the
# process exits, which Windows will not let the cleanup delete.
_TMP = tempfile.TemporaryDirectory(prefix="contextengine_selftest_", ignore_cleanup_errors=True)
get_settings().raw.setdefault("paths", {})["database"] = str(Path(_TMP.name) / "selftest.db")

from app.agent import state_machine  # noqa: E402
from app.context import embeddings as embeddings_module  # noqa: E402
from app.context.documents import build_documents  # noqa: E402
from app.context.store import get_store  # noqa: E402

CATALOG = "sales_dev"

TABLES = [
    {
        "schema_name": "raw",
        "table_name": "orders",
        "table_type": "MANAGED",
        "owner": "ingest@example.com",
        "comment": "Raw order headers landed from the commerce platform, one row per order.",
        "columns": [
            ("order_id", "STRING", "Unique order identifier from the commerce platform."),
            ("customer_id", "STRING", "Customer who placed the order."),
            ("order_status", "STRING", "One of placed, shipped, delivered, cancelled."),
            ("order_timestamp", "TIMESTAMP", "When the order was placed, UTC."),
            ("order_amount", "DECIMAL(18,2)", "Gross order value in INR before tax and discounts."),
            ("discount_amount", "DECIMAL(18,2)", "Total discount applied to the order in INR."),
        ],
    },
    {
        "schema_name": "raw",
        "table_name": "order_items",
        "table_type": "MANAGED",
        "owner": "ingest@example.com",
        "comment": "Order line items, one row per product within an order.",
        "columns": [
            ("order_id", "STRING", "Order this line belongs to."),
            ("product_id", "STRING", "Product sold on this line."),
            ("quantity", "INT", "Units sold on this line."),
            ("unit_price", "DECIMAL(18,2)", "Price per unit in INR at time of sale."),
        ],
    },
    {
        "schema_name": "raw",
        "table_name": "products",
        "table_type": "MANAGED",
        "owner": "ingest@example.com",
        "comment": "Product master with category and brand attributes.",
        "columns": [
            ("product_id", "STRING", "Product identifier."),
            ("product_name", "STRING", "Display name of the product."),
            ("category", "STRING", "Merchandising category."),
        ],
    },
    {
        "schema_name": "curated",
        "table_name": "order_revenue_daily",
        "table_type": "MANAGED",
        "owner": "analytics@example.com",
        "comment": "Daily net revenue per product, excluding cancelled orders.",
        "columns": [
            ("revenue_date", "DATE", "Calendar date of the order."),
            ("product_id", "STRING", "Product identifier."),
            ("net_revenue", "DECIMAL(18,2)", "Net revenue in INR after discounts."),
        ],
    },
    {
        "schema_name": "hr",
        "table_name": "payroll",
        "table_type": "MANAGED",
        "owner": "hr@example.com",
        "comment": "Employee payroll runs. Unrelated to commerce revenue.",
        "columns": [
            ("employee_id", "STRING", "Employee identifier."),
            ("revenue_share", "DECIMAL(18,2)", "Commission share paid, in INR."),
            ("pay_date", "DATE", "Date the payment was made."),
        ],
    },
]

EDGES = [
    ("raw.orders", "curated.order_revenue_daily"),
    ("raw.order_items", "curated.order_revenue_daily"),
    ("raw.products", "curated.order_revenue_daily"),
]


# ----------------------------------------------------------------------
# deterministic stand-ins
# ----------------------------------------------------------------------


def fake_vector(text: str, dim: int) -> np.ndarray:
    """Hashed bag of words. Not semantic, but similarity still tracks term
    overlap, which is enough to verify fusion and ranking wiring."""
    vector = np.zeros(dim, dtype=np.float32)
    for token in re.split(r"[^a-z0-9]+", text.lower()):
        if len(token) < 3:
            continue
        index = int(hashlib.md5(token.encode()).hexdigest(), 16) % dim
        vector[index] += 1.0
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


class StubEmbeddingClient:
    def __init__(self, spec, dim):
        self.spec = spec
        self.dimensions = dim
        self.tokens_used = 0
        self.api_key = "stub"

    def embed(self, texts):
        return np.array([fake_vector(t, self.dimensions) for t in texts], dtype=np.float32)

    def embed_one(self, text):
        return fake_vector(text, self.dimensions)

    @property
    def cost_usd(self):
        return 0.0


GENERATED_NOTEBOOK = '''\
# Databricks notebook source
# MAGIC %md
# MAGIC # Monthly product revenue
# MAGIC Net revenue per product per month, excluding cancelled orders.

# COMMAND ----------

import pyspark.sql.functions as F
from pyspark.sql import DataFrame, SparkSession


def transform(spark: SparkSession, raw_df: DataFrame) -> DataFrame:
    # Cancelled orders are still landed in raw.orders, so they have to be
    # excluded here rather than upstream.
    orders = raw_df.filter(F.col("order_status") != "cancelled")

    items = spark.table("sales_dev.raw.order_items")
    products = spark.table("sales_dev.raw.products")

    # One row per order line; order_amount lives on the header so it is not
    # summed here to avoid fanning it out across lines.
    lines = (
        orders.join(items, on="order_id", how="inner")
        .join(products, on="product_id", how="left")
        .withColumn("line_revenue", F.col("quantity") * F.col("unit_price"))
        .withColumn("revenue_month", F.date_trunc("month", F.col("order_timestamp")))
    )

    return (
        lines.groupBy("revenue_month", "product_id", "product_name", "category")
        .agg(
            F.sum("line_revenue").alias("net_revenue"),
            F.countDistinct("order_id").alias("order_count"),
        )
        .select(
            "revenue_month",
            "product_id",
            "product_name",
            "category",
            "net_revenue",
            "order_count",
        )
    )
'''

GENERATED_CONFIG = """\
project_name: monthly_product_revenue
transformation_notebook: transformation_notebook

source:
  type: delta_table
  catalog: sales_dev
  schema: raw
  table: orders

sink:
  type: table
  catalog: sales_dev
  schema: curated
  table: monthly_product_revenue
  write_mode: overwrite

schedule:
  quartz_cron_expression: 0 0 2 * * ?
  timezone_id: Asia/Kolkata
  max_workers: 4

enabled: true
"""


def stub_complete_json(stage, system, user, schema, **kwargs):
    if stage == "intent":
        return {
            "restated_goal": "Net revenue per product per month, excluding cancelled orders.",
            "project_name": "monthly_product_revenue",
            "search_queries": [
                "monthly revenue by product",
                "order amount unit price quantity product",
                "orders order items products",
            ],
            "mentioned_tables": [],
            "mentioned_columns": [],
            "metrics": ["net revenue"],
            "filters": ["exclude cancelled orders"],
            "grain": "one row per product per month",
            "time_range": "last 12 months",
            "clarifications": [],
        }

    if stage == "rerank":
        candidates = user["candidates"] if isinstance(user, dict) else []
        rankings = []
        for candidate in candidates:
            table = candidate["table_id"]
            relevance = 0.05 if ".hr." in table else 0.9 if ".raw." in table else 0.5
            rankings.append({"table_id": table, "relevance": relevance, "reason": "stub ranking"})
        return {"rankings": rankings}

    if stage == "plan":
        return {
            "project_name": "monthly_product_revenue",
            "summary": "Joins order headers to line items and products, excludes cancelled orders, "
            "and aggregates net revenue per product per month.",
            "steps": [{"title": "Filter", "detail": "Drop cancelled orders."}],
            "primary_source_table": "sales_dev.raw.orders",
            "additional_source_tables": ["sales_dev.raw.order_items", "sales_dev.raw.products"],
            "sink_table": "sales_dev.curated.monthly_product_revenue",
            "sink_write_mode": "overwrite",
            "merge_keys": [],
            "sink_columns": [
                {"name": "revenue_month", "data_type": "TIMESTAMP", "description": "First day of the order month."},
                {"name": "product_id", "data_type": "STRING", "description": "Product identifier."},
                {"name": "product_name", "data_type": "STRING", "description": "Product display name."},
                {"name": "category", "data_type": "STRING", "description": "Merchandising category."},
                {"name": "net_revenue", "data_type": "DECIMAL(18,2)", "description": "Net revenue in INR."},
                {"name": "order_count", "data_type": "BIGINT", "description": "Distinct orders contributing."},
            ],
            "transformation_outline": ["Filter cancelled", "Join items", "Aggregate by month and product"],
            "schedule_cron": "0 0 2 * * ?",
            "schedule_timezone": "Asia/Kolkata",
            "assumptions": ["Revenue is quantity x unit_price at the line level."],
            "open_questions": [],
        }

    if stage in ("codegen", "repair"):
        return {"config_yaml": GENERATED_CONFIG, "notebook_py": GENERATED_NOTEBOOK, "notes": "stub codegen"}

    raise AssertionError(f"unexpected stage {stage}")


# ----------------------------------------------------------------------
# seeding
# ----------------------------------------------------------------------


def seed() -> None:
    store = get_store()
    settings = get_settings()

    store.replace_catalog_tables(
        CATALOG, [{k: v for k, v in t.items() if k != "columns"} for t in TABLES]
    )
    store.replace_catalog_columns(
        CATALOG,
        [
            {
                "schema_name": t["schema_name"],
                "table_name": t["table_name"],
                "column_name": name,
                "data_type": dtype,
                "comment": comment,
                "ordinal": i + 1,
                "nullable": "YES",
            }
            for t in TABLES
            for i, (name, dtype, comment) in enumerate(t["columns"])
        ],
    )
    store.upsert_lineage_edges(
        [
            {
                "source_id": f"{CATALOG}.{src}",
                "target_id": f"{CATALOG}.{tgt}",
                "entity_type": "JOB",
                "entity_id": "1001",
                "occurrences": 5,
                "last_event": "2026-09-01T02:00:00",
            }
            for src, tgt in EDGES
        ]
    )
    store.prune_lineage_to_known_tables()

    docs = build_documents(store)
    store.upsert_docs(docs)

    spec = settings.embedding_model
    dim = int(settings.get("retrieval", "embedding_dimensions", default=768))
    store.upsert_embeddings(
        [(d["doc_id"], spec.id, dim, d["body_hash"], fake_vector(d["body"], dim).tobytes()) for d in docs]
    )
    embeddings_module.VectorIndex.invalidate()

    summary = store.snapshot_summary()
    print(
        f"seeded {summary['tables']} tables, {summary['columns']} columns, "
        f"{summary['lineage_edges']} lineage edges, {summary['embedded_docs']} embeddings"
    )


def install_stubs() -> None:
    import app.llm.client as llm_client

    settings = get_settings()
    dim = int(settings.get("retrieval", "embedding_dimensions", default=768))
    stub = StubEmbeddingClient(settings.embedding_model, dim)

    embeddings_module.get_embedding_client = lambda: stub
    # retrieval.py imports the embedding client by name at module load.
    import app.context.retrieval as retrieval

    retrieval.get_embedding_client = lambda: stub
    retrieval.rerank_cards.__globals__["complete_json"] = stub_complete_json
    llm_client.complete_json = stub_complete_json
    state_machine.complete_json = stub_complete_json


# ----------------------------------------------------------------------
# the run
# ----------------------------------------------------------------------


async def drive(session_id: str, action: str, payload: dict) -> list[dict]:
    events = []
    async for event in state_machine.run(session_id, action, payload):
        events.append(event)
        if event["type"] == "status":
            print(f"    ... {event['message']}")
        elif event["type"] == "error":
            print(f"    !!! {event['message']}")
    return events


async def main(do_publish: bool) -> int:
    seed()
    install_stubs()
    store = get_store()

    session_id = store.create_session("selftest", state_machine.INTAKE, {})
    failures: list[str] = []

    print("\n1. intake and discovery")
    await drive(session_id, "message", {"text": "Monthly net revenue by product, excluding cancelled orders."})
    state = store.get_session(session_id)["state"]
    ranked = [c["table_id"] for c in state["candidates"]]
    print(f"   candidates: {ranked}")
    if f"{CATALOG}.raw.orders" not in ranked:
        failures.append("retrieval did not surface sales_dev.raw.orders")
    if ranked and ranked[-1] != f"{CATALOG}.hr.payroll" and f"{CATALOG}.hr.payroll" in ranked:
        failures.append("the unrelated payroll table outranked a real candidate")

    print("\n2. table approval and planning")
    chosen = [f"{CATALOG}.raw.orders", f"{CATALOG}.raw.order_items", f"{CATALOG}.raw.products"]
    await drive(session_id, "approve_tables", {"table_ids": chosen})
    state = store.get_session(session_id)["state"]
    graph = state.get("projected_lineage") or {}
    sink_nodes = [n for n in graph.get("nodes", []) if n.get("role") == "sink"]
    proposed_edges = [e for e in graph.get("edges", []) if e.get("status") == "proposed"]
    print(f"   projected graph: {len(graph.get('nodes', []))} nodes, {len(proposed_edges)} proposed edges")
    if not sink_nodes:
        failures.append("projected lineage has no sink node")
    if len(proposed_edges) != len(chosen):
        failures.append("projected lineage is missing proposed edges")

    print("\n3. codegen and validation")
    await drive(session_id, "approve_plan", {})
    state = store.get_session(session_id)["state"]
    validation = (state.get("code") or {}).get("validation") or {}
    print(f"   validation ok={validation.get('ok')} errors={len(validation.get('errors', []))}")
    for finding in validation.get("errors", []):
        print(f"     error [{finding['check']}] {finding['message']}")
    for finding in validation.get("warnings", []):
        print(f"     warn  [{finding['check']}] {finding['message']}")
    if not validation.get("ok"):
        failures.append("generated code failed validation")

    print("\n4. code approval")
    await drive(session_id, "approve_code", {})
    session = store.get_session(session_id)
    print(f"   stage: {session['stage']}")
    if session["stage"] != state_machine.PUBLISH_READY:
        failures.append(f"expected publish_ready, got {session['stage']}")

    print("\n5. column reference check catches a bad column")
    from app.agent.validators import validate_project

    broken = GENERATED_NOTEBOOK.replace('F.col("order_status")', 'F.col("order_state")')
    result = validate_project("monthly_product_revenue", GENERATED_CONFIG, broken, source_tables=chosen)
    caught = [f for f in result.errors if f.check == "columns"]
    print(f"   {caught[0].message if caught else 'NOT CAUGHT'}")
    if not caught:
        failures.append("column existence check missed a nonexistent column")

    print("\n5b. file source columns are not required to exist on a joined catalog table")
    file_notebook = '''\
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F


def transform(spark: SparkSession, raw_df: DataFrame) -> DataFrame:
    sellers_df = raw_df.dropDuplicates(["seller_id"])
    customers_df = spark.table("sales_dev.raw.orders")
    joined_df = sellers_df.join(
        customers_df,
        sellers_df["seller_id"] == customers_df["customer_id"],
        how="left",
    )
    return joined_df.select(
        F.col("seller_id"),
        F.col("seller_city"),
        F.col("seller_state"),
        F.col("customer_id"),
    )
'''
    file_config = """\
project_name: olist_sellers_join
transformation_notebook: transformation_notebook
source:
  type: file
  path: abfss://datasets@example.dfs.core.windows.net/raw/olist_sellers_dataset.csv
  format: csv
  options:
    header: "true"
    inferSchema: "true"
sink:
  type: table
  catalog: sales_dev
  schema: raw
  table: olist_sellers_raw
  write_mode: overwrite
enabled: true
"""
    file_result = validate_project(
        "olist_sellers_join",
        file_config,
        file_notebook,
        source_tables=["sales_dev.raw.orders"],
        raw_df_columns=["seller_id", "seller_zip_code_prefix", "seller_city", "seller_state"],
    )
    file_errors = [f.message for f in file_result.errors if f.check == "columns"]
    print(f"   ok={file_result.ok} column_errors={file_errors}")
    if file_errors:
        failures.append("file-source seller columns were incorrectly failed against the catalog table")

    # Same situation as a live session: file source, no declared raw_df schema.
    untyped = validate_project(
        "olist_sellers_join",
        file_config,
        file_notebook,
        source_tables=["sales_dev.raw.orders"],
    )
    untyped_errors = [f for f in untyped.errors if f.check == "columns"]
    print(f"   untyped file source ok={untyped.ok} column_errors={len(untyped_errors)}")
    if untyped_errors:
        failures.append("file source without raw_df_columns still failed column checks")

    if do_publish:
        print("\n6. git publish")
        from app.delivery import publisher

        get_settings().raw["paths"]["framework_clone"] = str(Path(_TMP.name) / "framework_clone")
        git = publisher.publish_to_git("monthly_product_revenue", GENERATED_CONFIG, GENERATED_NOTEBOOK)
        print(f"   ok={git.ok} branch={git.branch} commit={git.commit}")
        print(f"   {git.detail}")
        if not git.ok:
            failures.append(f"git publish failed: {git.detail}")

    print()
    if failures:
        print(f"FAILED ({len(failures)})")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--publish" in sys.argv)))
