"""Prompt templates live as markdown next to this module so they can be
edited without touching Python. Placeholders use {{name}} and are replaced
literally, which keeps JSON and code samples in the templates safe from
str.format's brace handling.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


@lru_cache(maxsize=32)
def _read(name: str) -> str:
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"No prompt template named {name}.md in {PROMPT_DIR}")
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, **values: object) -> str:
    template = _read(name)

    def substitute(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"Prompt '{name}' expects a value for {{{{{key}}}}}")
        return str(values[key])

    return _PLACEHOLDER.sub(substitute, template)
