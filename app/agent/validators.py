"""Machine-checks generated projects before a human sees them.

The framework already ships the two CI scripts that gate every human PR, so
those are run verbatim against a staging directory shaped like
`projects/<name>/` rather than reimplemented. On top of that: a Python
syntax and undefined-name pass, and a cross-check that every column the
transform references actually exists on the tables it reads.

Failures come back as structured messages, which is what makes the repair
loop work - the model gets the validator's own words, not a summary.
"""
from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

from app.agent.artifacts import is_catalog_table
from app.context.framework_spec import CONFIG_FILENAME, NOTEBOOK_FILENAME, FrameworkUnavailable, framework_root
from app.context.store import Store, get_store

# Column names that always exist because the transform creates them, or that
# are Spark internals rather than source columns.
_ALWAYS_AVAILABLE = {"*", "row_hash", "is_current", "effective_start_date", "effective_end_date"}

_COLUMN_ACCESSORS = {"col", "column"}
_CREATORS = {"withColumn", "alias", "withColumnRenamed"}


@dataclass
class Finding:
    check: str
    severity: str  # "error" | "warning"
    message: str

    def as_dict(self) -> dict:
        return {"check": self.check, "severity": self.severity, "message": self.message}


@dataclass
class ValidationResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, check: str, severity: str, message: str) -> None:
        self.findings.append(Finding(check, severity, message))

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": [f.as_dict() for f in self.errors],
            "warnings": [f.as_dict() for f in self.warnings],
        }

    def feedback(self) -> str:
        """The text handed back to the model on a repair attempt."""
        lines = []
        for finding in self.errors:
            lines.append(f"[{finding.check}] {finding.message}")
        for finding in self.warnings:
            lines.append(f"[{finding.check}] (warning) {finding.message}")
        return "\n\n".join(lines)


# ----------------------------------------------------------------------
# individual checks
# ----------------------------------------------------------------------


def _check_syntax(notebook_py: str, result: ValidationResult) -> ast.Module | None:
    try:
        return ast.parse(notebook_py, filename=NOTEBOOK_FILENAME)
    except SyntaxError as exc:
        result.add("syntax", "error", f"{NOTEBOOK_FILENAME} line {exc.lineno}: {exc.msg}")
        return None


def _check_yaml_shape(project_name: str, config_yaml: str, result: ValidationResult) -> dict | None:
    try:
        config = yaml.safe_load(config_yaml)
    except yaml.YAMLError as exc:
        result.add("yaml", "error", f"{CONFIG_FILENAME} is not valid YAML: {exc}")
        return None
    if not isinstance(config, dict):
        result.add("yaml", "error", f"{CONFIG_FILENAME} must be a mapping at the top level.")
        return None
    if config.get("project_name") != project_name:
        result.add(
            "yaml",
            "error",
            f"project_name is '{config.get('project_name')}' but the project folder is "
            f"'{project_name}'; the driver notebook's %run path resolution depends on them matching.",
        )
    if config.get("transformation_notebook") != "transformation_notebook":
        result.add(
            "yaml",
            "error",
            "transformation_notebook must be the literal string 'transformation_notebook' "
            f"(got {config.get('transformation_notebook')!r}).",
        )
    return config


def _run_ci_script(script: str, staging: Path, result: ValidationResult, check_name: str) -> None:
    """Run one of the framework's CI validators against the staging tree.

    These are the same scripts .github/workflows/ci.yml runs on every PR, so
    passing here means passing there.
    """
    try:
        root = framework_root()
    except FrameworkUnavailable as exc:
        result.add(check_name, "warning", f"Skipped: {exc}")
        return

    script_path = root / "ci" / script
    if not script_path.exists():
        result.add(check_name, "warning", f"Skipped: {script_path} not found.")
        return

    # PYTHONUTF8 matters on Windows: the CI scripts read project files with
    # `Path.read_text()` and no explicit encoding, which picks up the ANSI
    # code page and chokes on the em dashes in the framework's own examples.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    try:
        proc = subprocess.run(
            [sys.executable, str(script_path), "--projects-dir", str(staging)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            cwd=str(root),
            env=env,
        )
    except subprocess.SubprocessError as exc:
        result.add(check_name, "warning", f"Could not run {script}: {exc}")
        return

    if proc.returncode == 0:
        return

    output = (proc.stdout + "\n" + proc.stderr).strip()
    # The scripts print one "FAIL <message>" line per problem; keep those
    # verbatim and fall back to the whole transcript if the format changes.
    failures = [line.strip()[5:].strip() for line in output.splitlines() if line.strip().startswith("FAIL")]
    if failures:
        for failure in failures:
            result.add(check_name, "error", _relativize(failure, staging))
    else:
        result.add(check_name, "error", _relativize(output, staging))


def _relativize(text: str, staging: Path) -> str:
    """Strip the temp directory out of validator messages so the model sees
    `projects/x/ingestion_config.yaml`, not a path that will not exist next run."""
    return text.replace(str(staging), "projects").replace(str(staging.parent), "")


def _check_pyflakes(notebook_py: str, result: ValidationResult) -> None:
    try:
        from pyflakes.api import check as pyflakes_check
        from pyflakes.reporter import Reporter
    except ImportError:
        result.add("pyflakes", "warning", "pyflakes is not installed; skipped name analysis.")
        return

    warnings_out, errors_out = io.StringIO(), io.StringIO()
    pyflakes_check(notebook_py, NOTEBOOK_FILENAME, Reporter(warnings_out, errors_out))

    for line in warnings_out.getvalue().splitlines():
        message = line.strip()
        if not message:
            continue
        # An undefined name inside transform() is a guaranteed runtime
        # failure; unused imports and shadowing are just noise to flag.
        severity = "error" if "undefined name" in message else "warning"
        result.add("pyflakes", severity, message)


def _check_contract(tree: ast.Module, result: ValidationResult) -> ast.FunctionDef | None:
    """Same three rules as ci/validate_transformation_contract.py, checked
    inline so the model gets the reason even when the CI script is skipped."""
    func = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "transform"),
        None,
    )
    if func is None:
        result.add(
            "contract",
            "error",
            f"{NOTEBOOK_FILENAME} has no top-level `def transform(spark, raw_df):`.",
        )
        return None

    params = len(func.args.posonlyargs) + len(func.args.args)
    if params != 2:
        result.add("contract", "error", f"`transform()` must take exactly 2 positional parameters, found {params}.")
    if not any(isinstance(n, ast.Return) and n.value is not None for n in ast.walk(func)):
        result.add("contract", "error", "`transform()` must return a DataFrame.")

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # docstring
        if isinstance(node, ast.Assign) and all(
            isinstance(t, ast.Name) and t.id.isupper() for t in node.targets
        ):
            continue  # module-level constant
        result.add(
            "contract",
            "error",
            f"{NOTEBOOK_FILENAME} line {node.lineno}: nothing may execute at import time - the file "
            "is inlined into the driver session with %run. Move this inside transform().",
        )
    return func


# ----------------------------------------------------------------------
# column existence
# ----------------------------------------------------------------------


class _ColumnUsage(ast.NodeVisitor):
    """Collects columns the transform reads and columns it creates."""

    def __init__(self) -> None:
        self.referenced: dict[str, int] = {}
        self.created: set[str] = set()
        self.extra_tables: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast API
        name = _callee_name(node)

        if name in _COLUMN_ACCESSORS and node.args:
            for literal in _string_literals(node.args[0]):
                self.referenced.setdefault(literal, node.lineno)

        elif name in {"withColumn", "withColumnRenamed"} and node.args:
            for literal in _string_literals(node.args[0]):
                if name == "withColumnRenamed":
                    self.referenced.setdefault(literal, node.lineno)
                else:
                    self.created.add(literal)
            if name == "withColumnRenamed" and len(node.args) > 1:
                self.created.update(_string_literals(node.args[1]))

        elif name == "alias" and node.args:
            self.created.update(_string_literals(node.args[0]))

        elif name in {"select", "drop", "groupBy", "groupby", "orderBy", "sort", "dropDuplicates"}:
            for arg in node.args:
                for literal in _string_literals(arg):
                    self.referenced.setdefault(literal, node.lineno)

        elif name == "table" and node.args:
            self.extra_tables.update(_string_literals(node.args[0]))

        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802 - ast API
        # df["seller_id"] after a join is the usual way to qualify a column.
        slice_node = node.slice
        if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
            self.referenced.setdefault(slice_node.value, node.lineno)
        self.generic_visit(node)


def _callee_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _string_literals(node: ast.AST) -> list[str]:
    """Literal strings in an argument, including the elements of a list or
    tuple literal - `.select("a", "b")` and `.dropDuplicates(["id"])` both
    need to be seen."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        out: list[str] = []
        for element in node.elts:
            out.extend(_string_literals(element))
        return out
    return []


def _check_columns(
    tree: ast.Module,
    source_tables: Iterable[str],
    result: ValidationResult,
    store: Store,
    raw_df_columns: Iterable[str] = (),
    external_source: bool = False,
) -> None:
    """Cross-check column names against the catalog snapshot.

    `raw_df` is not always a catalog table. A file / JDBC / Kafka / API
    source has no information_schema row, so seller_* columns on a CSV must
    not be reported as missing from the *other* table in the join. When the
    primary source is external, columns that are not on any known catalog
    table are assumed to come from raw_df unless we were given an explicit
    file schema (`raw_df_columns`) that they are also missing from.
    """
    usage = _ColumnUsage()
    usage.visit(tree)
    if not usage.referenced:
        return

    declared = list(dict.fromkeys(list(source_tables) + sorted(usage.extra_tables)))
    catalog_ids = [t for t in declared if is_catalog_table(t)]
    non_catalog = [t for t in declared if t not in catalog_ids]

    available: set[str] = set(_ALWAYS_AVAILABLE) | set(usage.created) | {c for c in raw_df_columns if c}
    known_tables: list[str] = []
    unknown_catalog: list[str] = []

    for tid in catalog_ids:
        columns = store.get_columns(tid)
        if not columns:
            unknown_catalog.append(tid)
            continue
        known_tables.append(tid)
        available.update(c["column_name"] for c in columns)

    unknown_labels = non_catalog + unknown_catalog
    if unknown_labels:
        result.add(
            "columns",
            "warning",
            "No snapshot columns for " + ", ".join(unknown_labels) + " - those columns are treated "
            "as belonging to raw_df (a file or other external source), not to the catalog tables.",
        )

    file_source = external_source or bool(non_catalog) or bool(list(raw_df_columns))
    if not known_tables and not file_source:
        return

    missing = sorted(
        name
        for name in usage.referenced
        if name not in available and not name.startswith("_") and _looks_like_column(name)
    )
    if not missing:
        return

    # We know the file schema, or every source is a catalog table: a missing
    # name is a real bug. Otherwise it almost certainly arrived on raw_df.
    if file_source and not list(raw_df_columns):
        catalog_label = ", ".join(known_tables) or "the confirmed catalog tables"
        for name in missing:
            result.add(
                "columns",
                "warning",
                f"Column '{name}' (line {usage.referenced[name]}) is not on {catalog_label}. "
                "Assuming it comes from raw_df / the file source.",
            )
        return

    known = sorted(available - _ALWAYS_AVAILABLE)
    tables_label = ", ".join(known_tables) if known_tables else "the declared sources"
    for name in missing:
        suggestion = _closest(name, known)
        hint = f" Did you mean '{suggestion}'?" if suggestion else ""
        result.add(
            "columns",
            "error",
            f"Column '{name}' (line {usage.referenced[name]}) does not exist on any of "
            f"{tables_label}.{hint}",
        )


def _looks_like_column(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def _closest(name: str, options: list[str]) -> str | None:
    import difflib

    matches = difflib.get_close_matches(name, options, n=1, cutoff=0.75)
    return matches[0] if matches else None


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------


def validate_project(
    project_name: str,
    config_yaml: str,
    notebook_py: str,
    source_tables: Iterable[str] = (),
    raw_df_columns: Iterable[str] = (),
    store: Store | None = None,
    run_ci: bool = True,
) -> ValidationResult:
    store = store or get_store()
    result = ValidationResult()

    tree = _check_syntax(notebook_py, result)
    config = _check_yaml_shape(project_name, config_yaml, result)
    source_type = ((config or {}).get("source") or {}).get("type")
    external_source = bool(source_type) and source_type != "delta_table"

    if tree is not None:
        _check_contract(tree, result)
        _check_pyflakes(notebook_py, result)
        _check_columns(
            tree,
            source_tables,
            result,
            store,
            raw_df_columns=raw_df_columns,
            external_source=external_source,
        )

    if run_ci:
        with tempfile.TemporaryDirectory(prefix="contextengine_validate_") as tmp:
            staging = Path(tmp) / "projects"
            project_dir = staging / project_name
            project_dir.mkdir(parents=True)
            (project_dir / CONFIG_FILENAME).write_text(config_yaml, encoding="utf-8")
            (project_dir / NOTEBOOK_FILENAME).write_text(notebook_py, encoding="utf-8")

            _run_ci_script("validate_yaml_configs.py", staging, result, "ci_config_schema")
            _run_ci_script("validate_transformation_contract.py", staging, result, "ci_transform_contract")

    return result
