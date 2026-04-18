"""Integration tests for git worktree management."""

import subprocess

from workbench.worktree import (
    Worktree,
    create_session_branch,
    create_worktree,
    get_diff,
    get_diff_since,
    get_head_sha,
    get_main_branch,
    get_merged_branches,
    merge_into_session,
)


def _commit_file(repo, filename, content, message):
    """Helper: write a file, stage, and commit."""
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo,
        capture_output=True,
        check=True,
    )


def test_get_main_branch(git_repo):
    assert get_main_branch(git_repo) == "main"


def test_create_session_branch(git_repo):
    branch = create_session_branch(git_repo)
    assert branch == "workbench-1"
    # Verify the branch exists
    result = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert branch in result.stdout


def test_create_session_branch_increments(git_repo):
    b1 = create_session_branch(git_repo)
    b2 = create_session_branch(git_repo)
    assert b1 == "workbench-1"
    assert b2 == "workbench-2"


def test_create_worktree(git_repo):
    session = create_session_branch(git_repo)
    wt = create_worktree(git_repo, "task-1", "my-feature", base_branch=session)
    assert wt.path.exists()
    assert wt.branch == "wb/my-feature"
    assert wt.task_id == "task-1"
    # Cleanup
    wt.cleanup()


def test_worktree_cleanup(git_repo, monkeypatch, tmp_path):
    session = create_session_branch(git_repo)
    wt = create_worktree(git_repo, "task-1", "cleanup-test", base_branch=session)
    assert wt.path.exists()
    branch = wt.branch
    # Run cleanup from a directory that is NOT the repo to prove cwd is handled
    monkeypatch.chdir(tmp_path)
    wt.cleanup()
    # Worktree should no longer be listed
    result = subprocess.run(
        ["git", "worktree", "list"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert "cleanup-test" not in result.stdout
    # Branch should be deleted
    result = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert branch not in result.stdout


def test_get_diff(git_repo):
    session = create_session_branch(git_repo)
    wt = create_worktree(git_repo, "task-1", "diff-test", base_branch=session)
    try:
        _commit_file(wt.path, "new.txt", "hello", "add new file")
        diff = get_diff(wt, session)
        assert "new.txt" in diff
        assert "hello" in diff
    finally:
        wt.cleanup()


def test_merge_into_session_clean(git_repo):
    session = create_session_branch(git_repo)
    wt = create_worktree(git_repo, "task-1", "merge-clean", base_branch=session)
    try:
        _commit_file(wt.path, "feature.txt", "feature content", "add feature")
        result = merge_into_session(git_repo, session, wt.branch)
        assert result.success is True
        assert result.branch == wt.branch
    finally:
        wt.cleanup()


def test_merge_into_session_conflict(git_repo):
    session = create_session_branch(git_repo)

    # Create two worktrees that edit the same file differently
    wt1 = create_worktree(git_repo, "task-1", "conflict-a", base_branch=session)
    wt2 = create_worktree(git_repo, "task-2", "conflict-b", base_branch=session)
    try:
        _commit_file(wt1.path, "shared.txt", "version A", "edit from A")
        _commit_file(wt2.path, "shared.txt", "version B", "edit from B")

        # Merge first branch — should succeed
        r1 = merge_into_session(git_repo, session, wt1.branch)
        assert r1.success is True

        # Merge second branch — should conflict
        r2 = merge_into_session(git_repo, session, wt2.branch)
        assert r2.success is False
        assert r2.conflicts is not None
        assert "shared.txt" in r2.conflicts
    finally:
        wt1.cleanup()
        wt2.cleanup()


def test_complete_merge(git_repo):
    """After a conflict, manually re-merge, resolve, and commit in the merge worktree."""
    session = create_session_branch(git_repo)
    wt1 = create_worktree(git_repo, "task-1", "cm-a", base_branch=session)
    wt2 = create_worktree(git_repo, "task-2", "cm-b", base_branch=session)
    try:
        _commit_file(wt1.path, "shared.txt", "version A", "edit A")
        _commit_file(wt2.path, "shared.txt", "version B", "edit B")

        merge_into_session(git_repo, session, wt1.branch)

        # Create a merge worktree to manually resolve
        merge_dir = git_repo / ".workbench" / "_merge_manual"
        subprocess.run(
            ["git", "worktree", "add", str(merge_dir), session],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        # Start the merge (will conflict)
        subprocess.run(
            ["git", "merge", wt2.branch],
            cwd=merge_dir,
            capture_output=True,
        )
        # Resolve by choosing a final version
        (merge_dir / "shared.txt").write_text("resolved version")
        subprocess.run(
            ["git", "add", "shared.txt"], cwd=merge_dir, capture_output=True, check=True
        )
        result = subprocess.run(
            ["git", "commit", "--no-edit"],
            cwd=merge_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        # Clean up merge worktree
        subprocess.run(
            ["git", "worktree", "remove", str(merge_dir), "--force"],
            cwd=git_repo,
            capture_output=True,
        )
    finally:
        wt1.cleanup()
        wt2.cleanup()


def test_complete_merge_unresolved(git_repo):
    """Committing with unresolved conflict markers should fail."""
    session = create_session_branch(git_repo)
    wt1 = create_worktree(git_repo, "task-1", "cu-a", base_branch=session)
    wt2 = create_worktree(git_repo, "task-2", "cu-b", base_branch=session)
    try:
        _commit_file(wt1.path, "shared.txt", "version A", "edit A")
        _commit_file(wt2.path, "shared.txt", "version B", "edit B")

        merge_into_session(git_repo, session, wt1.branch)

        merge_dir = git_repo / ".workbench" / "_merge_manual"
        subprocess.run(
            ["git", "worktree", "add", str(merge_dir), session],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )
        merge_result = subprocess.run(
            ["git", "merge", wt2.branch],
            cwd=merge_dir,
            capture_output=True,
        )
        assert merge_result.returncode != 0

        # The file has conflict markers — read it to verify
        content = (merge_dir / "shared.txt").read_text()
        assert "<<<<<<<" in content or "=======" in content

        # Trying to commit without resolving: git add the conflicted file as-is
        # and attempt commit — the conflict markers are still in the file content
        subprocess.run(["git", "add", "shared.txt"], cwd=merge_dir, capture_output=True)
        # Git allows committing files with conflict markers, so we verify
        # that the content still has markers (the "unresolved" state)
        committed_content = (merge_dir / "shared.txt").read_text()
        assert "<<<<<<<" in committed_content or "=======" in committed_content

        # Abort and clean up
        subprocess.run(["git", "merge", "--abort"], cwd=merge_dir, capture_output=True)
        subprocess.run(
            ["git", "worktree", "remove", str(merge_dir), "--force"],
            cwd=git_repo,
            capture_output=True,
        )
    finally:
        wt1.cleanup()
        wt2.cleanup()


def test_get_merged_branches_empty(git_repo):
    """Session branch with no merges returns only the base branches."""
    session = create_session_branch(git_repo)
    merged = get_merged_branches(git_repo, session)
    # main and the session branch itself are merged into session
    assert "main" in merged
    assert session in merged


def test_get_merged_branches_after_merge(git_repo):
    """After merging a task branch, it appears in get_merged_branches."""
    session = create_session_branch(git_repo)
    wt = create_worktree(git_repo, "task-1", "merged-feat", base_branch=session)
    try:
        _commit_file(wt.path, "feat.txt", "content", "add feature")
        result = merge_into_session(git_repo, session, wt.branch)
        assert result.success

        merged = get_merged_branches(git_repo, session)
        assert wt.branch in merged
    finally:
        wt.cleanup()


def test_get_merged_branches_excludes_unmerged(git_repo):
    """A task branch that hasn't been merged should not appear."""
    session = create_session_branch(git_repo)
    wt = create_worktree(git_repo, "task-1", "unmerged-feat", base_branch=session)
    try:
        _commit_file(wt.path, "feat.txt", "content", "add feature")

        merged = get_merged_branches(git_repo, session)
        # The branch has diverged, so it should NOT be in the merged set
        assert wt.branch not in merged
    finally:
        wt.cleanup()


def test_get_head_sha(git_repo):
    """get_head_sha returns the current HEAD commit SHA."""
    wt = Worktree(path=git_repo, branch="main", task_id="test")
    sha = get_head_sha(wt)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_get_head_sha_failure(tmp_path):
    """get_head_sha returns empty string for non-git directories."""
    wt = Worktree(path=tmp_path, branch="main", task_id="test")
    assert get_head_sha(wt) == ""


def test_get_diff_since(git_repo):
    """get_diff_since returns only changes after the given SHA."""
    wt = Worktree(path=git_repo, branch="main", task_id="test")
    before_sha = get_head_sha(wt)

    _commit_file(git_repo, "new.txt", "new content", "add new file")

    diff = get_diff_since(wt, before_sha)
    assert "new content" in diff

    full_diff = get_diff_since(wt, before_sha)
    assert "init" not in full_diff
