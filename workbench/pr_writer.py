"""PR-writer orchestration: invokes the pr_writer agent and parses its output."""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from workbench.adapters import get_adapter
from workbench.directives import PrWriterDirective
from workbench.tmux import run_in_tmux

if TYPE_CHECKING:
    from workbench.profile import Profile


_TITLE_MAX = 200
_BODY_MIN = 10


class PrWriterError(Exception):
    """Base error for PR-writer failures (recoverable)."""


class PrWriterAgentError(PrWriterError):
    """The agent exited non-zero or didn't write the file."""


class PrWriterParseError(PrWriterError):
    """The file was written but doesn't match the expected format."""


async def run_pr_writer(
    repo: Path,
    session_branch: str,
    plan_slug: str,
    base_branch: str,
    plan_source: Path,
    merged_task_titles: list[str],
    agent_cmd: str = "claude",
    use_tmux: bool = True,
    profile: Profile | None = None,
    directive_override: str | None = None,
    agents_config_paths: list[Path] | None = None,
    wrap_up_dir: Path | None = None,
) -> tuple[str, str]:
    """Run the PR-writer agent and return (title, body).

    Writes the artifact to .workbench/<plan_slug>/wrap-up/<session_branch>/pr-body.md.
    On parse failure, raises PrWriterParseError.
    On agent crash (non-zero exit), raises PrWriterAgentError.
    Both errors are recoverable — callers fall back to plan-derived defaults.
    """
    if not plan_source.exists():
        raise FileNotFoundError(f"Plan source not found: {plan_source}")

    plan_content = plan_source.read_text(encoding="utf-8")
    plan_excerpt = _extract_plan_excerpt(plan_content)

    diff_stat = await _run_git(
        repo, ["git", "diff", "--stat", f"{base_branch}...{session_branch}"]
    )
    commit_log = await _run_git(
        repo, ["git", "log", f"{base_branch}..{session_branch}", "--oneline"]
    )

    directive_text = _resolve_directive_text(
        directive_override, profile, PrWriterDirective.DEFAULT_TEXT
    )
    agent_cmd_resolved = _resolve_agent_cmd(agent_cmd, profile)

    if wrap_up_dir is None:
        wrap_up_dir = repo / ".workbench" / plan_slug / "wrap-up" / session_branch
    wrap_up_dir.mkdir(parents=True, exist_ok=True)
    output_path = wrap_up_dir / "pr-body.md"
    output_path.unlink(missing_ok=True)

    directive = PrWriterDirective(
        directive_text=directive_text,
        plan_excerpt=plan_excerpt,
        diff_stat=diff_stat,
        commit_log=commit_log,
        merged_tasks=merged_task_titles,
        output_path=output_path,
    )
    prompt = directive.render()

    adapter_paths = (
        agents_config_paths
        if agents_config_paths is not None
        else [repo / ".workbench" / "agents.yaml"]
    )
    adapter = get_adapter(agent_cmd_resolved, adapter_paths)

    # The agent must run inside a checkout of session_branch so its Read/Glob/Bash
    # tools see the actual code being described. Running from `repo` (typically
    # on main) would show stale content. Create an ephemeral worktree and clean
    # it up afterwards; the bare git store at `repo` already serviced the
    # diff/log queries above.
    wt_path = wrap_up_dir / ".pr-writer-wt"
    try:
        _create_pr_writer_worktree(repo, wt_path, session_branch)
    except Exception as e:  # noqa: BLE001
        raise PrWriterAgentError(f"Could not create PR-writer worktree: {e}") from e

    try:
        try:
            cmd = adapter.build_command(prompt, wt_path)
            if use_tmux:
                session_name = f"wb-pr-writer-{plan_slug}"
                returncode, raw_output = await run_in_tmux(session_name, cmd, wt_path)
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(wt_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                returncode = proc.returncode
                raw_output = stdout.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 — wrap adapter/subprocess errors
            raise PrWriterAgentError(f"Agent invocation failed: {e}") from e

        if returncode != 0:
            raise PrWriterAgentError(f"PR-writer agent exited {returncode}:\n{raw_output[:2000]}")
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise PrWriterAgentError("Agent did not write pr-body.md")

        return _parse_pr_body(output_path)
    finally:
        _cleanup_pr_writer_worktree(repo, wt_path)


# ---------------------------------------------------------------------------
# Plan-content helpers (used by callers as a fallback when the writer fails)
# ---------------------------------------------------------------------------


def derive_title_from_plan(plan_content: str, plan_slug: str) -> str:
    """Return the plan's H1 line, falling back to title-cased slug."""
    for line in plan_content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return plan_slug.replace("-", " ").title()


def derive_body_from_plan(
    plan_content: str,
    merged_task_titles: list[str],
    review_path: Path | None = None,
) -> str:
    """Build a plan-derived PR body from the plan's Context section and merged tasks.

    The body has up to three sections: the ``## Context`` text from the plan
    (if present), a ``## Tasks`` bullet list of merged_task_titles, and an
    optional reviewer footer when review_path is provided.
    """
    context_lines: list[str] = []
    in_context = False
    for line in plan_content.splitlines():
        if line.strip() == "## Context":
            in_context = True
            continue
        if in_context:
            if line.startswith("## "):
                break
            context_lines.append(line)

    parts: list[str] = []
    context_text = "\n".join(context_lines).strip()
    if context_text:
        parts.append(f"## Context\n\n{context_text}")

    if merged_task_titles:
        tasks_section = "## Tasks\n\n" + "\n".join(f"- {t}" for t in merged_task_titles)
        parts.append(tasks_section)

    if review_path is not None:
        parts.append(f"🤖 Reviewed by workbench. Report: `{review_path}`")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_plan_excerpt(plan_content: str) -> str:
    """Return the plan H1 plus its ``## Context`` section (if any)."""
    h1: str | None = None
    context_lines: list[str] = []
    in_context = False
    for line in plan_content.splitlines():
        if h1 is None and line.startswith("# "):
            h1 = line.rstrip()
            continue
        if line.strip() == "## Context":
            in_context = True
            context_lines.append(line)
            continue
        if in_context:
            if line.startswith("## "):
                break
            context_lines.append(line)

    parts: list[str] = []
    if h1:
        parts.append(h1)
    context_text = "\n".join(context_lines).rstrip()
    if context_text:
        parts.append(context_text)
    return "\n\n".join(parts).strip()


def _resolve_directive_text(
    cli_override: str | None,
    profile: Profile | None,
    default_text: str,
) -> str:
    """Resolve directive text: CLI override > profile.pr_writer > default."""
    if cli_override:
        return cli_override
    if profile is not None:
        cfg = getattr(profile, "pr_writer", None)
        if cfg and cfg.directive:
            return cfg.directive
    return default_text


def _resolve_agent_cmd(agent_cmd: str, profile: Profile | None) -> str:
    """Resolve agent command: profile.pr_writer.agent (if default) > agent_cmd."""
    if agent_cmd != "claude":
        return agent_cmd
    if profile is not None:
        cfg = getattr(profile, "pr_writer", None)
        if cfg and cfg.agent and cfg.agent != "claude":
            return cfg.agent
    return agent_cmd


async def _run_git(repo: Path, cmd: list[str]) -> str:
    """Run a git subprocess in `repo`. Raise PrWriterAgentError on non-zero exit."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception as e:  # noqa: BLE001
        raise PrWriterAgentError(f"git invocation failed ({' '.join(cmd)}): {e}") from e

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise PrWriterAgentError(
            f"git command failed ({' '.join(cmd)}, exit={proc.returncode}): {err}"
        )
    return stdout.decode("utf-8", errors="replace")


def _parse_pr_body(output_path: Path) -> tuple[str, str]:
    """Parse pr-body.md. First non-blank line is `# <title>`; rest is body."""
    content = output_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    title_idx: int | None = None
    title: str | None = None
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        match = re.match(r"^#\s+(.+)$", line)
        if not match:
            raise PrWriterParseError("First non-blank line of pr-body.md must be `# <title>`")
        title = match.group(1).strip()
        title_idx = idx
        break

    if title is None or title_idx is None:
        raise PrWriterParseError("pr-body.md has no title heading")

    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 1] + "…"

    body = "\n".join(lines[title_idx + 1 :]).strip()
    if len(body) < _BODY_MIN:
        raise PrWriterParseError("pr-body.md has empty or too-short body")

    return title, body


def _create_pr_writer_worktree(repo: Path, wt_path: Path, session_branch: str) -> None:
    """Create a git worktree at wt_path checked out to session_branch's tip.

    Uses --detach because the PR writer only reads the tree to describe the
    diff. A non-detached `git worktree add <path> <branch>` fails with exit
    128 when the branch is already checked out elsewhere (the main repo
    typically sits on the session branch right after a run).
    """
    if wt_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", str(wt_path), "--force"],
            cwd=repo,
            capture_output=True,
        )
    result = subprocess.run(
        ["git", "worktree", "add", "--detach", str(wt_path), session_branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git worktree add (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip() or 'no output'}"
        )


def _cleanup_pr_writer_worktree(repo: Path, wt_path: Path) -> None:
    """Remove the PR-writer worktree (best-effort)."""
    if wt_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", str(wt_path), "--force"],
            cwd=repo,
            capture_output=True,
        )
