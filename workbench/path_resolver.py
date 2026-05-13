"""Path resolution helpers for plan-scoped config and artifacts."""

from __future__ import annotations

from pathlib import Path


def is_plan_reference_path(arg: str) -> bool:
    """True if arg looks like a path (contains '/' or '\\' or ends in '.md')."""
    return "/" in arg or "\\" in arg or arg.endswith(".md")


def resolve_plan_path(arg: str, repo: Path) -> Path:
    """Resolve a plan reference to a plan.md path.

    Resolution rules:
    1. If arg is a path (per is_plan_reference_path): return ``Path(arg)``
       verbatim. Callers that need an absolute path should call ``.resolve()``
       themselves (the cli wrapper does this).
    2. Otherwise: treat arg as a plan name. Try in order:
       a. repo / ".workbench" / arg / "plan.md"  (new layout)
       b. repo / ".workbench" / "plans" / f"{arg}.md"  (legacy layout)
    3. Raise FileNotFoundError with a clear message if nothing matches.
    """
    if is_plan_reference_path(arg):
        return Path(arg)

    new_layout = repo / ".workbench" / arg / "plan.md"
    if new_layout.exists():
        return new_layout

    legacy_layout = repo / ".workbench" / "plans" / f"{arg}.md"
    if legacy_layout.exists():
        return legacy_layout

    raise FileNotFoundError(
        f"No plan found for name {arg!r}. Looked in " f"{new_layout} and {legacy_layout}."
    )


def plan_slug_from_path(plan_path: Path) -> str:
    """Given a plan.md path, return the canonical plan slug.

    New layout: parent folder name (.workbench/<slug>/plan.md → <slug>)
    Legacy layout: file stem (.workbench/plans/<slug>.md → <slug>)
    Other paths: fall back to file stem.

    A bare ``plan.md`` whose parent is the cwd resolves the path so the
    slug becomes the cwd's directory name rather than an empty string.
    """
    if plan_path.name == "plan.md":
        parent_name = plan_path.parent.name
        if not parent_name:
            parent_name = plan_path.resolve().parent.name
        return parent_name
    return plan_path.stem


def resolve_profile_paths(
    repo: Path,
    plan_slug: str | None = None,
    explicit_path: Path | None = None,
    name: str | None = None,
) -> list[Path]:
    """Return profile.yaml paths to consult, in precedence order (highest first).

    Order:
    1. explicit_path (when not None) — always wins, single-element list.
    2. repo / ".workbench" / plan_slug / f"profile{suffix}.yaml" (when plan_slug)
    3. repo / ".workbench" / f"profile{suffix}.yaml"
    4. ~/.workbench/f"profile{suffix}.yaml"
    """
    if explicit_path is not None:
        return [explicit_path]

    suffix = f".{name}" if name else ""
    filename = f"profile{suffix}.yaml"

    candidates: list[Path] = []
    if plan_slug:
        candidates.append(repo / ".workbench" / plan_slug / filename)
    candidates.append(repo / ".workbench" / filename)
    candidates.append(Path.home() / ".workbench" / filename)

    return [p for p in candidates if p.exists()]


def resolve_agents_config_paths(
    repo: Path,
    plan_slug: str | None = None,
) -> list[Path]:
    """Return agents.yaml paths to consult, in precedence order (highest first).

    Order:
    1. repo / ".workbench" / plan_slug / "agents.yaml" (when plan_slug)
    2. repo / ".workbench" / "agents.yaml"
    """
    candidates: list[Path] = []
    if plan_slug:
        candidates.append(repo / ".workbench" / plan_slug / "agents.yaml")
    candidates.append(repo / ".workbench" / "agents.yaml")

    return [p for p in candidates if p.exists()]
