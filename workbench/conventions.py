"""Project-level conventions file (.workbench/conventions.md) helpers."""

from __future__ import annotations

import re
from pathlib import Path

CONVENTIONS_FILENAME = "conventions.md"
_CONVENTIONS_HEADING = re.compile(r"^##\s+Conventions\s*$", re.MULTILINE | re.IGNORECASE)

TEMPLATE = """\
# Project conventions

Conventions shared across all workbench plans in this repo. Plans may override by
adding their own `## Conventions` section — that section wins.

## Code style
- (e.g., line length, formatter, type-hint policy)

## Testing
- (e.g., test runner, naming conventions, coverage expectations)

## Git
- (e.g., commit message format, branch naming, PR style)

## Naming
- (e.g., file names, variable names, classes/functions)

## Other
- (anything else worth standardizing)
"""


def conventions_path(repo: Path) -> Path:
    """Return the canonical conventions file path for a repo."""
    return repo / ".workbench" / CONVENTIONS_FILENAME


def load_conventions_text(repo: Path) -> str:
    """Return the stripped contents of `.workbench/conventions.md`, or '' if absent/empty."""
    path = conventions_path(repo)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def apply_conventions_fallback(plan_text: str, repo: Path) -> str:
    """Return plan_text with a `## Conventions` section appended from the file when needed.

    Returns plan_text unchanged when the text already contains a `## Conventions`
    heading, the file does not exist, or the file is empty / whitespace-only.
    Idempotent.
    """
    if _CONVENTIONS_HEADING.search(plan_text):
        return plan_text
    content = load_conventions_text(repo)
    if not content:
        return plan_text
    return f"{plan_text.rstrip()}\n\n## Conventions\n\n{content}\n"
