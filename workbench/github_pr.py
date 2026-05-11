"""GitHub PR creation via the gh CLI."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path


async def create_pr(
    repo: Path,
    session_branch: str,
    base_branch: str,
    title: str,
    body: str,
) -> tuple[bool, str]:
    """Create a GitHub PR via `gh pr create`.

    Returns (success, message). On success, message is the PR URL.
    On failure, message is either:
      - A copy-pasteable `gh pr create ...` command (when gh is missing/unauthed)
      - The stderr from gh (when gh ran but failed)
    """
    if not _gh_available():
        body_path = _write_body_file(repo, body)
        return False, _format_suggested_command(
            session_branch,
            base_branch,
            title,
            str(body_path),
        )

    if not await _gh_authenticated():
        body_path = _write_body_file(repo, body)
        return False, _format_suggested_command(
            session_branch,
            base_branch,
            title,
            str(body_path),
        )

    body_path = _write_body_file(repo, body)
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "pr",
            "create",
            "--base",
            base_branch,
            "--head",
            session_branch,
            "--title",
            title,
            "--body-file",
            str(body_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(repo),
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            pr_url = stdout.decode().strip().splitlines()[-1]
            return True, pr_url
        else:
            return False, stderr.decode().strip()
    finally:
        body_path.unlink(missing_ok=True)


def _gh_available() -> bool:
    """Check if `gh` is on PATH."""
    return shutil.which("gh") is not None


async def _gh_authenticated() -> bool:
    """Check if `gh auth status` reports a logged-in account."""
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "auth",
        "status",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0


def _write_body_file(repo: Path, body: str) -> Path:
    """Write the PR body to a tempfile under .workbench/tmp/."""
    tmp_dir = repo / ".workbench" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    body_path = tmp_dir / "pr-body.md"
    body_path.write_text(body, encoding="utf-8")
    return body_path


def _format_suggested_command(
    session_branch: str,
    base_branch: str,
    title: str,
    body_file_path: str,
) -> str:
    """Return a suggested gh pr create command for the user to run manually."""
    return (
        f"gh pr create"
        f" --base {base_branch}"
        f" --head {session_branch}"
        f" --title {title!r}"
        f" --body-file {body_file_path}"
    )
