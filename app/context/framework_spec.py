"""Compiles what the model needs to know about the ingestion framework.

The framework contract is small, static and unforgiving: two files with
fixed names, a pydantic schema with per-source-type conditional rules, and a
`transform(spark, raw_df)` signature that CI rejects any deviation from.
That makes it the wrong thing to RAG over - retrieval can silently drop the
one required field that makes a config valid. Instead the whole contract is
compiled once into a spec card and sent as a cacheable system block.

The card is derived from the framework's own source (the pydantic model's
JSON schema and the validator functions verbatim), so it cannot drift from
what the framework actually enforces.
"""
from __future__ import annotations

import importlib
import inspect
import json
import logging
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import get_settings

log = logging.getLogger(__name__)

CONFIG_FILENAME = "ingestion_config.yaml"
NOTEBOOK_FILENAME = "transformation_notebook.py"


class FrameworkUnavailable(RuntimeError):
    pass


def framework_root() -> Path:
    root = get_settings().framework_repo
    if not root.exists():
        raise FrameworkUnavailable(
            f"paths.framework_repo points at {root}, which does not exist."
        )
    return root


def _ensure_importable(root: Path) -> None:
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


@lru_cache(maxsize=1)
def load_config_module():
    """Import the framework's own config_loader so validation and schema
    generation use the framework's definitions, not a copy of them."""
    root = framework_root()
    _ensure_importable(root)
    try:
        return importlib.import_module("ingestion_framework.config.config_loader")
    except Exception as exc:  # noqa: BLE001
        raise FrameworkUnavailable(
            f"Could not import ingestion_framework.config.config_loader from {root / 'src'}: {exc}"
        ) from exc


def config_json_schema() -> dict:
    module = load_config_module()
    return module.IngestionConfig.model_json_schema()


def validator_source() -> str:
    """The conditional rules verbatim. These are the parts a JSON schema
    cannot express (source.type=jdbc needs jdbc_url_secret plus one of
    table/query, and so on), and they are exactly what generated configs
    fail on most often."""
    module = load_config_module()
    chunks = []
    for name in ("SourceConfig", "SinkConfig", "IngestionConfig"):
        cls = getattr(module, name, None)
        if cls is None:
            continue
        for attr, member in vars(cls).items():
            if not attr.startswith("_check"):
                continue
            fn = getattr(member, "__func__", member)
            wrapped = getattr(fn, "__wrapped__", fn)
            try:
                chunks.append(inspect.getsource(wrapped).rstrip())
            except (OSError, TypeError):
                continue
    return "\n\n".join(chunks)


# ----------------------------------------------------------------------
# example projects
# ----------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_examples() -> list[dict]:
    root = framework_root()
    projects_dir = root / "projects"
    examples: list[dict] = []
    if not projects_dir.exists():
        return examples

    for child in sorted(projects_dir.iterdir()):
        config_path = child / CONFIG_FILENAME
        notebook_path = child / NOTEBOOK_FILENAME
        if not (config_path.exists() and notebook_path.exists()):
            continue
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        examples.append(
            {
                "project_name": child.name,
                "source_type": (config.get("source") or {}).get("type"),
                "sink_type": (config.get("sink") or {}).get("type", "table"),
                "write_mode": (config.get("sink") or {}).get("write_mode"),
                "config_yaml": config_path.read_text(encoding="utf-8"),
                "notebook_py": notebook_path.read_text(encoding="utf-8"),
                "config": config,
            }
        )
    return examples


def select_examples(source_type: str | None, sink_type: str | None, write_mode: str | None, limit: int = 2) -> list[dict]:
    """Nearest examples by shape, not by text. A project reading a Delta
    table and merging SCD2 learns far more from another SCD2 project than
    from the highest-scoring embedding match."""
    examples = load_examples()
    if not examples:
        return []

    def score(ex: dict) -> int:
        points = 0
        if source_type and ex["source_type"] == source_type:
            points += 4
        if sink_type and ex["sink_type"] == sink_type:
            points += 3
        if write_mode and ex["write_mode"] == write_mode:
            points += 2
        if ex["write_mode"] and str(ex["write_mode"]).startswith("merge") and write_mode and str(write_mode).startswith("merge"):
            points += 1
        return points

    ranked = sorted(examples, key=score, reverse=True)
    return ranked[:limit]


# ----------------------------------------------------------------------
# the card
# ----------------------------------------------------------------------

_CONTRACT = f"""\
# Target framework: azure-databricks-ingestion-framework

A project is exactly two files in `projects/<project_name>/`:

  * `{CONFIG_FILENAME}`   - declares the source and the sink
  * `{NOTEBOOK_FILENAME}` - declares the business logic

Everything else (reading, writing, merging, auditing, job creation) is the
framework's job. Never generate framework code, readers, writers, or a
Spark session.

## Hard rules, all enforced by CI

1. `project_name` in the YAML must equal the folder name.
2. `transformation_notebook` in the YAML must be the string
   `transformation_notebook` (the filename without `.py`).
3. `{NOTEBOOK_FILENAME}` must define a top-level function named exactly
   `transform` taking exactly two positional parameters `(spark, raw_df)`
   and containing a `return` of a DataFrame. Not a method, not nested,
   not a lambda.
4. The file runs through `%run` inside the driver notebook's session, so it
   must not create a SparkSession, must not call `display()`/`dbutils` at
   import time, and must not execute anything at module level beyond
   imports and function definitions.
5. `raw_df` already contains the source data described by the YAML. Do not
   re-read the source inside `transform`. The returned DataFrame is written
   to the sink by the framework.
6. Credentials never appear in YAML - only `*_secret` keys naming an entry
   in the Databricks secret scope.
7. When `sink.write_mode` is `merge_scd2`, `transform` must produce the
   SCD2 columns the writer expects: `row_hash`, `is_current`,
   `effective_start_date`, `effective_end_date`.
8. A `delta_table` source is read as a whole table. Filter and join inside
   `transform`; use `spark.table("catalog.schema.table")` for any *extra*
   tables beyond the one declared as the source.
"""


@lru_cache(maxsize=1)
def _static_card() -> str:
    schema = json.dumps(config_json_schema(), indent=2)
    rules = validator_source()
    parts = [_CONTRACT, "## Config schema (generated from the framework's pydantic model)", "```json", schema, "```"]
    if rules:
        parts += [
            "## Conditional rules the schema cannot express (framework source, verbatim)",
            "```python",
            rules,
            "```",
        ]
    return "\n\n".join(parts)


def spec_card(
    source_type: str | None = None,
    sink_type: str | None = None,
    write_mode: str | None = None,
    include_examples: bool = True,
) -> str:
    """The full cacheable system block for any stage that reasons about the
    framework. Roughly 3-4k tokens, identical across turns of a session, so
    providers with prompt caching bill it at the cached rate after turn one.
    """
    parts = [_static_card()]
    if include_examples:
        for ex in select_examples(source_type, sink_type, write_mode):
            parts.append(
                f"## Example project `{ex['project_name']}` "
                f"(source={ex['source_type']}, sink={ex['sink_type']}, write_mode={ex['write_mode']})\n\n"
                f"`{CONFIG_FILENAME}`:\n```yaml\n{ex['config_yaml'].strip()}\n```\n\n"
                f"`{NOTEBOOK_FILENAME}`:\n```python\n{ex['notebook_py'].strip()}\n```"
            )
    return "\n\n".join(parts)


def job_template() -> dict:
    path = framework_root() / "jobs" / "job_template.json"
    if not path.exists():
        raise FrameworkUnavailable(f"Job template not found at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def existing_project_names() -> list[str]:
    projects = framework_root() / "projects"
    if not projects.exists():
        return []
    return sorted(p.name for p in projects.iterdir() if p.is_dir() and not p.name.startswith("__"))


def describe() -> dict[str, Any]:
    """Diagnostics for the health endpoint and the UI's context panel."""
    try:
        schema = config_json_schema()
        source_types = (
            schema.get("$defs", {}).get("SourceConfig", {}).get("properties", {}).get("type", {}).get("enum")
        )
        sink_types = (
            schema.get("$defs", {}).get("SinkConfig", {}).get("properties", {}).get("type", {}).get("enum")
        )
        return {
            "available": True,
            "root": str(framework_root()),
            "source_types": source_types,
            "sink_types": sink_types,
            "examples": [e["project_name"] for e in load_examples()],
            "spec_card_chars": len(spec_card()),
        }
    except FrameworkUnavailable as exc:
        return {"available": False, "error": str(exc)}
