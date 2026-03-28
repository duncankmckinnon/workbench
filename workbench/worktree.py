"""Git worktree management for isolated agent work."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Worktree:
    """A git worktree for an agent to work in."""
    path: Path
    branch: str
    task_id: str

    def cleanup(self):
        """Remove the worktree and delete the branch."""
        subprocess.run(
            ["git", "worktree", "remove", str(self.path), "--force"],
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", self.branch],
            capture_output=True,
        )


def get_main_branch(repo: Path) -> str:
    """Detect the main/default branch."""
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip().split("/")[-1]

    # Fallback: check for main or master
    result = subprocess.run(
        ["git", "branch", "--list", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if "main" in result.stdout:
        return "main"
    return "master"


def create_session_branch(repo: Path) -> str:
    """Create a clean session branch off main (workbench-1, workbench-2, etc)."""
    base = get_main_branch(repo)

    # Find the next available session number
    result = subprocess.run(
        ["git", "branch", "--list", "workbench-*"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    existing = []
    for line in result.stdout.strip().split("\n"):
        name = line.strip().lstrip("* ")
        if name.startswith("workbench-"):
            try:
                num = int(name.split("-", 1)[1])
                existing.append(num)
            except ValueError:
                pass

    next_num = max(existing, default=0) + 1
    session_branch = f"workbench-{next_num}"

    subprocess.run(
        ["git", "branch", session_branch, base],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    return session_branch


def create_worktree(repo: Path, task_id: str, task_slug: str, base_branch: str | None = None) -> Worktree:
    """Create an isolated worktree for a task.

    Args:
        repo: Path to the git repo root.
        task_id: Unique task identifier.
        task_slug: Slug for the branch name.
        base_branch: Branch to create the worktree from. Defaults to main.
    """
    branch = f"wb/{task_slug}"
    base = base_branch or get_main_branch(repo)
    worktree_dir = repo / ".workbench" / task_id

    # Create the branch from the base
    subprocess.run(
        ["git", "branch", branch, base],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    # Create the worktree
    subprocess.run(
        ["git", "worktree", "add", str(worktree_dir), branch],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    return Worktree(path=worktree_dir, branch=branch, task_id=task_id)


@dataclass
class MergeResult:
    """Result of merging a task branch into the session branch."""
    branch: str
    success: bool
    message: str
    conflicts: list[str] | None = None
    merge_dir: Path | None = None


def merge_into_session(
    repo: Path,
    session_branch: str,
    task_branch: str,
    cleanup_on_conflict: bool = False,
) -> MergeResult:
    """Merge a task branch into the session branch.

    Uses a temporary worktree to avoid disturbing the main working tree.

    When cleanup_on_conflict is False (default), the merge worktree is left
    in place on conflict so a resolver agent can be dispatched into it.
    When True, conflicts are aborted and the worktree is cleaned up immediately.
    """
    merge_dir = repo / ".workbench" / "_merge"

    # Create a temporary worktree on the session branch
    subprocess.run(
        ["git", "worktree", "remove", str(merge_dir), "--force"],
        cwd=repo, capture_output=True,
    )
    add_wt = subprocess.run(
        ["git", "worktree", "add", str(merge_dir), session_branch],
        cwd=repo, capture_output=True, text=True,
    )
    if add_wt.returncode != 0:
        return MergeResult(
            branch=task_branch, success=False,
            message=f"Failed to create merge worktree: {add_wt.stderr}",
        )

    # Attempt the merge inside the worktree
    merge = subprocess.run(
        ["git", "merge", task_branch, "-m", f"Merge {task_branch} into {session_branch}"],
        cwd=merge_dir, capture_output=True, text=True,
    )

    if merge.returncode == 0:
        # Clean merge — clean up worktree
        subprocess.run(
            ["git", "worktree", "remove", str(merge_dir), "--force"],
            cwd=repo, capture_output=True,
        )
        return MergeResult(branch=task_branch, success=True, message="Merged cleanly.")

    # Merge conflict — collect conflicting files
    status = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=merge_dir, capture_output=True, text=True,
    )
    conflicts = [f.strip() for f in status.stdout.strip().split("\n") if f.strip()]

    if cleanup_on_conflict:
        # Abort and clean up (old behavior)
        subprocess.run(["git", "merge", "--abort"], cwd=merge_dir, capture_output=True)
        subprocess.run(
            ["git", "worktree", "remove", str(merge_dir), "--force"],
            cwd=repo, capture_output=True,
        )
        return MergeResult(
            branch=task_branch, success=False,
            message=f"Merge conflict in {len(conflicts)} file(s).",
            conflicts=conflicts,
        )

    # Leave merge worktree in place for resolver agent
    return MergeResult(
        branch=task_branch, success=False,
        message=f"Merge conflict in {len(conflicts)} file(s).",
        conflicts=conflicts,
        merge_dir=merge_dir,
    )


def complete_merge(merge_dir: Path, repo: Path, session_branch: str, task_branch: str) -> MergeResult:
    """Complete a merge after conflicts have been resolved by staging and committing.

    Checks that no conflict markers remain, commits the merge, and cleans up
    the temporary merge worktree.
    """
    # Check for remaining conflict markers in tracked files
    check = subprocess.run(
        ["git", "diff", "--check"],
        cwd=merge_dir, capture_output=True, text=True,
    )
    if check.returncode != 0:
        return MergeResult(
            branch=task_branch, success=False,
            message=f"Conflict markers remain: {check.stdout.strip()}",
        )

    # Commit the resolved merge
    commit = subprocess.run(
        ["git", "commit", "--no-edit"],
        cwd=merge_dir, capture_output=True, text=True,
    )
    if commit.returncode != 0:
        return MergeResult(
            branch=task_branch, success=False,
            message=f"Merge commit failed: {commit.stderr.strip()}",
        )

    # Clean up the merge worktree
    subprocess.run(
        ["git", "worktree", "remove", str(merge_dir), "--force"],
        cwd=repo, capture_output=True,
    )

    return MergeResult(
        branch=task_branch, success=True,
        message=f"Merged {task_branch} into {session_branch} (conflicts resolved).",
    )


def cleanup_merge_worktree(repo: Path, merge_dir: Path) -> None:
    """Abort merge and remove the merge worktree."""
    subprocess.run(["git", "merge", "--abort"], cwd=merge_dir, capture_output=True)
    subprocess.run(
        ["git", "worktree", "remove", str(merge_dir), "--force"],
        cwd=repo, capture_output=True,
    )


def get_diff(worktree: Worktree, base_branch: str) -> str:
    """Get the diff of changes made in a worktree."""
    result = subprocess.run(
        ["git", "diff", f"{base_branch}...HEAD"],
        cwd=worktree.path,
        capture_output=True,
        text=True,
    )
    return result.stdout
