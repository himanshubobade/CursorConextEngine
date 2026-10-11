"""Delivers a generated project to the framework repository and to Databricks.

Two independent paths, run together but reported separately, because they
fail for unrelated reasons and either one is useful on its own:

  * git   - a branch and commit in a working clone, which is where the
            change gets reviewed and where the framework's own CI would run
  * workspace - a direct upload to /Workspace/Shared/..., which is what
            makes the project runnable right now

The clone matters: the reference checkout configured as `paths.framework_repo`
is never written to. `git clone` from it into `paths.framework_clone` gives a
working copy that inherits the same history and remotes without touching the
original.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.context.databricks_client import DatabricksError, get_client
from app.context.framework_spec import CONFIG_FILENAME, NOTEBOOK_FILENAME, framework_root

log = logging.getLogger(__name__)

PACKAGE_MARKER = "__init__.py"
WORKSPACE_NOTEBOOK_NAME = NOTEBOOK_FILENAME.removesuffix(".py")


class PublishError(RuntimeError):
    pass


@dataclass
class GitResult:
    ok: bool
    branch: str | None = None
    commit: str | None = None
    pushed: bool = False
    remote: str | None = None
    detail: str = ""
    files: list[str] | None = None
    mode: str | None = None


def _git(repo: Path, *args: str, check: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError as exc:
        raise PublishError("git is not on PATH, so the repository path cannot be used.") from exc
    except subprocess.SubprocessError as exc:
        raise PublishError(f"git {' '.join(args)} failed: {exc}") from exc

    if check and proc.returncode != 0:
        raise PublishError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return (proc.stdout or "").strip()


SEED_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", ".venv", "venv", "node_modules", ".pytest_cache", ".mypy_cache"
)


def clone_mode() -> str:
    """How the working clone can be created from the reference checkout.

    `clone` only works when the framework directory is itself a git
    repository root. In this workspace it is a subdirectory of a larger
    repository, which `git clone` refuses - so the fallback seeds a
    standalone repository from a copy of the tree instead. Either way the
    reference checkout is only ever read.
    """
    return "clone" if (framework_root() / ".git").exists() else "seed"


def _seed_clone(source: Path, clone: Path) -> None:
    shutil.copytree(source, clone, ignore=SEED_IGNORE, dirs_exist_ok=True)
    _git(clone, "init", "-b", "main")
    _git(clone, "add", "-A")
    _git(
        clone,
        "-c", "user.name=ContextEngine",
        "-c", "user.email=contextengine@local",
        "commit",
        "-m",
        "Baseline: snapshot of azure-databricks-ingestion-framework\n\n"
        "Created by ContextEngine so generated projects can be committed and reviewed "
        "without writing to the reference checkout.",
    )


def ensure_clone() -> Path:
    """Working clone of the framework, created on first use.

    Delete the directory to re-seed it from the current reference checkout.
    """
    settings = get_settings()
    clone = settings.framework_clone
    source = framework_root()

    if (clone / ".git").exists():
        return clone

    clone.parent.mkdir(parents=True, exist_ok=True)
    mode = clone_mode()

    if mode == "clone":
        try:
            subprocess.run(
                ["git", "clone", str(source), str(clone)],
                capture_output=True,
                text=True,
                timeout=600,
                check=True,
            )
        except FileNotFoundError as exc:
            raise PublishError("git is not on PATH, so the repository path cannot be used.") from exc
        except subprocess.CalledProcessError as exc:
            raise PublishError(
                f"Could not clone {source} into {clone}: {(exc.stderr or exc.stdout).strip()}"
            ) from exc

        # Cloning a local path sets that path as `origin`, and a later push
        # would then write into the reference checkout. Rename it out of the
        # way and re-point `origin` at the real upstream, if there was one.
        _git(clone, "remote", "rename", "origin", "reference", check=False)
        upstream = _git(source, "remote", "get-url", "origin", check=False)
        if upstream:
            _git(clone, "remote", "add", "origin", upstream, check=False)
    else:
        try:
            _seed_clone(source, clone)
        except (OSError, PublishError) as exc:
            shutil.rmtree(clone, ignore_errors=True)
            raise PublishError(f"Could not seed a working repository at {clone}: {exc}") from exc

    log.info("created framework working repository at %s (mode=%s)", clone, mode)
    return clone


def default_branch(clone: Path) -> str:
    branch = _git(clone, "symbolic-ref", "--short", "HEAD", check=False)
    return branch or "main"


def project_files(project_name: str, config_yaml: str, notebook_py: str) -> dict[str, str]:
    base = f"projects/{project_name}"
    return {
        f"{base}/{CONFIG_FILENAME}": config_yaml,
        f"{base}/{NOTEBOOK_FILENAME}": notebook_py,
        f"{base}/{PACKAGE_MARKER}": "",
    }


def preview(project_name: str, config_yaml: str, notebook_py: str) -> dict:
    """What publishing would write, and whether anything already exists."""
    settings = get_settings()
    files = project_files(project_name, config_yaml, notebook_py)
    reference = framework_root()
    workspace_root = settings.workspace_repo_root
    return {
        "project_name": project_name,
        "git": {
            "clone": str(settings.framework_clone),
            "mode": clone_mode(),
            "clone_exists": (settings.framework_clone / ".git").exists(),
            "exists_in_reference": (reference / "projects" / project_name).exists(),
            "files": [{"path": path, "bytes": len(content.encode("utf-8"))} for path, content in files.items()],
        },
        "workspace": {
            "root": workspace_root,
            "paths": [
                f"{workspace_root}/projects/{project_name}/{WORKSPACE_NOTEBOOK_NAME}",
                f"{workspace_root}/projects/{project_name}/{CONFIG_FILENAME}",
            ],
        },
    }


def publish_to_git(project_name: str, config_yaml: str, notebook_py: str, branch: str | None = None) -> GitResult:
    try:
        mode = clone_mode()
        clone = ensure_clone()
    except PublishError as exc:
        return GitResult(ok=False, detail=str(exc))

    branch = branch or f"feat/{project_name}"
    files = project_files(project_name, config_yaml, notebook_py)

    try:
        base = default_branch(clone)
        _git(clone, "checkout", base, check=False)
        # Re-publishing the same project should land on the same branch.
        existing = _git(clone, "rev-parse", "--verify", branch, check=False)
        if existing:
            _git(clone, "checkout", branch)
        else:
            _git(clone, "checkout", "-b", branch)

        for relative, content in files.items():
            path = clone / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")

        _git(clone, "add", *files.keys())
        status = _git(clone, "status", "--porcelain", check=False)
        if not status:
            commit = _git(clone, "rev-parse", "--short", "HEAD", check=False)
            return GitResult(
                ok=True,
                branch=branch,
                commit=commit,
                mode=mode,
                detail="No changes to commit - the files already match what is on this branch.",
                files=list(files),
            )

        _git(
            clone,
            "-c", "user.name=ContextEngine",
            "-c", "user.email=contextengine@local",
            "commit",
            "-m",
            f"Add ingestion project {project_name}\n\nGenerated by ContextEngine.",
        )
        commit = _git(clone, "rev-parse", "--short", "HEAD")

        remote = _git(clone, "remote", "get-url", "origin", check=False)
        pushed = False
        push_detail = ""
        if remote:
            try:
                _git(clone, "push", "-u", "origin", branch)
                pushed = True
            except PublishError as exc:
                push_detail = f" Commit succeeded but push failed: {exc}"
        else:
            push_detail = (
                f" This is a local commit only - the working repository at {clone} has no 'origin' "
                "remote. Add one with `git remote add origin <url>` to enable pushing."
            )

        return GitResult(
            ok=True,
            branch=branch,
            commit=commit,
            pushed=pushed,
            remote=remote or None,
            mode=mode,
            detail=f"Committed {len(files)} files to {branch}.{push_detail}",
            files=list(files),
        )
    except PublishError as exc:
        return GitResult(ok=False, branch=branch, mode=mode, detail=str(exc))


def publish_to_workspace(project_name: str, config_yaml: str, notebook_py: str) -> dict:
    client = get_client()
    if not client.token:
        return {"ok": False, "detail": "No Databricks token configured."}

    root = get_settings().workspace_repo_root.rstrip("/")
    target_dir = f"{root}/projects/{project_name}"
    notebook_path = f"{target_dir}/{WORKSPACE_NOTEBOOK_NAME}"
    uploaded: list[str] = []
    try:
        client.mkdirs(target_dir)
        # The driver does `%run ../projects/<name>/transformation_notebook`.
        # workspace/import keeps the path verbatim, so the notebook must be
        # imported without the .py suffix or %run cannot resolve it.
        client.upload_file(notebook_path, notebook_py, language="PYTHON")
        uploaded.append(notebook_path)
        client.workspace_delete(f"{target_dir}/{NOTEBOOK_FILENAME}")
        client.upload_file(f"{target_dir}/{CONFIG_FILENAME}", config_yaml)
        uploaded.append(f"{target_dir}/{CONFIG_FILENAME}")
        return {
            "ok": True,
            "directory": target_dir,
            "files": uploaded,
            "detail": f"Uploaded {len(uploaded)} files to {target_dir}.",
        }
    except DatabricksError as exc:
        return {"ok": False, "directory": target_dir, "files": uploaded, "detail": str(exc)}


def publish(project_name: str, config_yaml: str, notebook_py: str, branch: str | None = None) -> dict:
    git_result = publish_to_git(project_name, config_yaml, notebook_py, branch)
    workspace_result = publish_to_workspace(project_name, config_yaml, notebook_py)
    return {
        "project_name": project_name,
        "git": git_result.__dict__,
        "workspace": workspace_result,
        "ok": git_result.ok or workspace_result.get("ok", False),
    }
