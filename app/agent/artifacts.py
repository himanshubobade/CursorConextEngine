"""Output schemas for the structured stages.

Every field is a scalar, a string, or an array of flat objects. Nested
free-form objects are deliberately avoided: the three providers accept
different JSON Schema dialects, and a config block is carried as YAML text
anyway since that is the artifact we ultimately write to disk.
"""
from __future__ import annotations

import re
from typing import Any

INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "restated_goal": {
            "type": "string",
            "description": "One or two sentences restating what the analyst asked for, in data terms.",
        },
        "project_name": {
            "type": "string",
            "description": "snake_case identifier for the new project folder, e.g. monthly_product_revenue.",
        },
        "search_queries": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-4 distinct retrieval queries covering the entities, metrics and grain mentioned.",
        },
        "mentioned_tables": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Table names the analyst named explicitly, verbatim.",
        },
        "mentioned_columns": {"type": "array", "items": {"type": "string"}},
        "metrics": {"type": "array", "items": {"type": "string"}},
        "filters": {"type": "array", "items": {"type": "string"}},
        "grain": {"type": "string", "description": "One row per what, if stated or inferable."},
        "time_range": {"type": "string"},
        "clarifications": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Questions worth asking only if the request is genuinely ambiguous. Usually empty.",
        },
    },
    "required": ["restated_goal", "project_name", "search_queries"],
}


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_name": {"type": "string"},
        "summary": {"type": "string", "description": "Two or three sentences an analyst can check."},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["title", "detail"],
            },
        },
        "source_type": {
            "type": "string",
            "enum": ["jdbc", "autoloader", "kafka", "api", "delta_table", "file"],
            "description": "Config source.type. Use file for a one-shot CSV/JSON/Parquet read from ADLS.",
        },
        "source_path": {
            "type": "string",
            "description": "abfss:// or wasbs:// path when source_type is file or autoloader.",
        },
        "source_format": {
            "type": "string",
            "description": "csv, json, parquet, orc, avro, or text when source_type is file.",
        },
        "primary_source_table": {
            "type": "string",
            "description": "For delta_table: catalog.schema.table. For file: the ADLS path (same as source_path).",
        },
        "additional_source_tables": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Other catalog tables joined inside transform() via spark.table(...).",
        },
        "raw_df_columns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "data_type": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name"],
            },
            "description": "Columns that arrive on raw_df. Required for file/csv sources; they are not in Unity Catalog.",
        },
        "sink_table": {"type": "string", "description": "Fully qualified catalog.schema.table to write."},
        "sink_write_mode": {
            "type": "string",
            "enum": ["append", "overwrite", "merge_scd1", "merge_scd2"],
        },
        "merge_keys": {"type": "array", "items": {"type": "string"}},
        "partition_columns": {"type": "array", "items": {"type": "string"}},
        "sink_columns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "data_type": {"type": "string"},
                    "description": {"type": "string", "description": "Becomes the Unity Catalog column comment."},
                },
                "required": ["name", "data_type", "description"],
            },
        },
        "transformation_outline": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ordered operations transform() will perform.",
        },
        "schedule_cron": {"type": "string", "description": "Quartz cron, e.g. '0 0 2 * * ?'."},
        "schedule_timezone": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "project_name",
        "summary",
        "steps",
        "primary_source_table",
        "sink_table",
        "sink_write_mode",
        "sink_columns",
        "transformation_outline",
    ],
}


CODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "config_yaml": {
            "type": "string",
            "description": "Full contents of ingestion_config.yaml.",
        },
        "notebook_py": {
            "type": "string",
            "description": "Full contents of transformation_notebook.py, including the Databricks cell markers.",
        },
        "notes": {
            "type": "string",
            "description": "Short note on anything the reviewer should look at closely.",
        },
    },
    "required": ["config_yaml", "notebook_py"],
}


COLUMN_DOCS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "description"],
            },
        }
    },
    "required": ["columns"],
}


_CATALOG_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_catalog_table(name: str | None) -> bool:
    """True only for a three-level Unity Catalog name.

    File paths, abfss URLs and JDBC identifiers must not be treated as
    catalog tables - they have no information_schema row, so a column check
    against them would look at the wrong table and fail the pipeline.
    """
    if not name or "://" in name or "/" in name or "\\" in name:
        return False
    parts = name.split(".")
    return len(parts) == 3 and all(_CATALOG_PART.fullmatch(p) for p in parts)


def plan_sources(plan: dict) -> list[str]:
    """Every identifier the plan reads, primary first, deduplicated."""
    tables = [plan.get("primary_source_table")] + list(plan.get("additional_source_tables") or [])
    return [t for i, t in enumerate(tables) if t and t not in tables[:i]]


def plan_catalog_sources(plan: dict) -> list[str]:
    """Catalog tables only - file paths are excluded."""
    return [t for t in plan_sources(plan) if is_catalog_table(t)]


def plan_raw_df_columns(plan: dict) -> list[str]:
    names: list[str] = []
    for col in plan.get("raw_df_columns") or []:
        if isinstance(col, str):
            names.append(col)
        elif isinstance(col, dict) and col.get("name"):
            names.append(str(col["name"]))
    return names
