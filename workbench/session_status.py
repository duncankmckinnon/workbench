"""Persistent session status tracking.

Writes per-task status to ``.workbench/<plan>/status.yaml`` so that
``--only-incomplete`` can reliably skip already-completed tasks,
even when the process was interrupted mid-wave.

Files are named by plan slug. Inside each file, sessions are keyed
by session branch, so multiple runs of the same plan coexist.

Legacy layout (``.workbench/status-<plan>.yaml``) is still read as a
fallback, but new writes always use the per-plan folder layout.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class TaskRecord:
    """Persisted outcome for a single task."""

    status: str  # "done" | "failed" | "pending"
    branch: str | None = None
    merged: bool = False
    last_agent: str = ""
    completed_stages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "branch": self.branch,
            "merged": self.merged,
            "last_agent": self.last_agent,
            "completed_stages": list(self.completed_stages),
        }

    @classmethod
    def from_dict(cls, data: dict) -> TaskRecord:
        return cls(
            status=data.get("status", "pending"),
            branch=data.get("branch"),
            merged=data.get("merged", False),
            last_agent=data.get("last_agent", ""),
            completed_stages=list(data.get("completed_stages", [])),
        )


@dataclass
class FinalReviewRecord:
    """One persisted final-review run."""

    timestamp: str  # ISO 8601 UTC, e.g. "2026-05-10T14:22:31Z"
    verdict: str  # "pass" | "fail" | "error"
    review_path: str  # repo-relative path to review.md
    requirements_path: str  # repo-relative path to requirements.md
    summarizer_agent: str  # e.g. "claude"
    reviewer_agent: str  # e.g. "claude"
    cost_usd: float = 0.0
    pr_url: str | None = None

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "verdict": self.verdict,
            "review_path": self.review_path,
            "requirements_path": self.requirements_path,
            "summarizer_agent": self.summarizer_agent,
            "reviewer_agent": self.reviewer_agent,
            "cost_usd": self.cost_usd,
            "pr_url": self.pr_url,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FinalReviewRecord:
        return cls(
            timestamp=data.get("timestamp", ""),
            verdict=data.get("verdict", ""),
            review_path=data.get("review_path", data.get("report_path", "")),
            requirements_path=data.get("requirements_path", ""),
            summarizer_agent=data.get("summarizer_agent", ""),
            reviewer_agent=data.get("reviewer_agent", ""),
            cost_usd=data.get("cost_usd", 0.0),
            pr_url=data.get("pr_url"),
        )


@dataclass
class SessionStatus:
    """Read/write session status to disk.

    File: ``.workbench/<plan_slug>/status.yaml``
    Structure::

        plan_source: /path/to/plan.md
        sessions:
          workbench-1:
            tasks:
              task-1: {status: done, branch: wb/feat-a, merged: true, last_agent: reviewer}
              task-2: {status: failed, branch: wb/feat-b, merged: false, last_agent: implementor}
          workbench-2:
            tasks:
              task-1: ...
    """

    plan_slug: str
    session_branch: str
    plan_source: str = ""
    tasks: dict[str, TaskRecord] = field(default_factory=dict)
    final_reviews: list[FinalReviewRecord] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # -- persistence ----------------------------------------------------------

    @staticmethod
    def path_for(repo: Path, plan_slug: str) -> Path:
        return repo / ".workbench" / plan_slug / "status.yaml"

    @staticmethod
    def legacy_path_for(repo: Path, plan_slug: str) -> Path:
        return repo / ".workbench" / f"status-{plan_slug}.yaml"

    def save(self, repo: Path) -> None:
        """Synchronous save — call from non-async code or when lock is already held.

        Loads the full file first to preserve other sessions, then writes
        back with this session's data updated.
        """
        path = self.path_for(repo, self.plan_slug)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing data to preserve other sessions
        existing = {}
        if path.exists():
            existing = yaml.safe_load(path.read_text()) or {}

        sessions = existing.get("sessions", {})
        session_data: dict = {
            "tasks": {tid: rec.to_dict() for tid, rec in self.tasks.items()},
        }
        if self.final_reviews:
            session_data["final_reviews"] = [r.to_dict() for r in self.final_reviews]
        sessions[self.session_branch] = session_data

        data = {"sessions": sessions}
        if self.plan_source:
            data["plan_source"] = self.plan_source
        elif "plan_source" in existing:
            data["plan_source"] = existing["plan_source"]
        path.write_text(yaml.dump(data, default_flow_style=False))

    async def update_task(
        self,
        repo: Path,
        task_id: str,
        status: str,
        branch: str | None = None,
        merged: bool = False,
        last_agent: str = "",
        completed_stages: list[str] | None = None,
    ) -> None:
        """Atomically record a task and save to disk under a lock."""
        async with self._lock:
            self.record_task(task_id, status, branch, merged, last_agent, completed_stages)
            self.save(repo)

    async def update_merged(self, repo: Path, task_id: str) -> None:
        """Atomically mark a task merged and save under a lock."""
        async with self._lock:
            self.mark_merged(task_id)
            self.save(repo)

    async def append_final_review(self, repo: Path, record: FinalReviewRecord) -> None:
        """Atomically append a final-review record and save under a lock."""
        async with self._lock:
            self.final_reviews.append(record)
            self.save(repo)

    @classmethod
    def load(cls, repo: Path, plan_slug: str, session_branch: str) -> SessionStatus | None:
        """Load status for a specific plan and session branch.

        Tries the new per-plan folder layout first, then falls back to
        the legacy flat-file path. Returns None if neither exists or
        the session branch is not found in the file.
        """
        path = cls.path_for(repo, plan_slug)
        if not path.exists():
            path = cls.legacy_path_for(repo, plan_slug)
            if not path.exists():
                return None
        data = yaml.safe_load(path.read_text()) or {}
        sessions = data.get("sessions", {})
        session_data = sessions.get(session_branch)
        if session_data is None:
            return None
        tasks = {
            tid: TaskRecord.from_dict(rec) for tid, rec in session_data.get("tasks", {}).items()
        }
        final_reviews = [
            FinalReviewRecord.from_dict(r) for r in session_data.get("final_reviews", [])
        ]
        return cls(
            plan_slug=plan_slug,
            session_branch=session_branch,
            plan_source=data.get("plan_source", ""),
            tasks=tasks,
            final_reviews=final_reviews,
        )

    @classmethod
    def find_by_session(cls, repo: Path, session_branch: str) -> SessionStatus | None:
        """Find a session across all status files.

        Scans both new (``<plan>/status.yaml``) and legacy
        (``status-*.yaml``) layouts. New layout wins when both exist
        for the same plan slug.

        Returns the matching ``SessionStatus``, or None if no match is
        found. Raises ``ValueError`` if multiple status files contain
        the same session branch.
        """
        wb_dir = repo / ".workbench"
        if not wb_dir.exists():
            return None

        # Collect (slug -> (path, data)) with new layout taking precedence
        slug_entries: dict[str, tuple[Path, dict]] = {}

        # Legacy layout: status-<slug>.yaml
        for path in sorted(wb_dir.glob("status-*.yaml")):
            slug = path.stem.removeprefix("status-")
            data = yaml.safe_load(path.read_text()) or {}
            slug_entries[slug] = (path, data)

        # New layout: <slug>/status.yaml — overwrites legacy if both exist
        for path in sorted(wb_dir.glob("*/status.yaml")):
            slug = path.parent.name
            data = yaml.safe_load(path.read_text()) or {}
            slug_entries[slug] = (path, data)

        matches: list[SessionStatus] = []
        for slug, (_path, data) in slug_entries.items():
            sessions = data.get("sessions", {})
            if session_branch in sessions:
                session_data = sessions[session_branch]
                tasks = {
                    tid: TaskRecord.from_dict(rec)
                    for tid, rec in session_data.get("tasks", {}).items()
                }
                final_reviews = [
                    FinalReviewRecord.from_dict(r) for r in session_data.get("final_reviews", [])
                ]
                matches.append(
                    cls(
                        plan_slug=slug,
                        session_branch=session_branch,
                        plan_source=data.get("plan_source", ""),
                        tasks=tasks,
                        final_reviews=final_reviews,
                    )
                )

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            slugs = [m.plan_slug for m in matches]
            raise ValueError(
                f"Session '{session_branch}' found in multiple status files: "
                f"{', '.join(slugs)}. Specify the plan path explicitly."
            )
        return None

    # -- task-level updates ---------------------------------------------------

    def record_task(
        self,
        task_id: str,
        status: str,
        branch: str | None = None,
        merged: bool = False,
        last_agent: str = "",
        completed_stages: list[str] | None = None,
    ) -> None:
        if completed_stages is None:
            existing = self.tasks.get(task_id)
            completed_stages = list(existing.completed_stages) if existing else []
        self.tasks[task_id] = TaskRecord(
            status=status,
            branch=branch,
            merged=merged,
            last_agent=last_agent,
            completed_stages=list(completed_stages),
        )

    def mark_merged(self, task_id: str) -> None:
        if task_id in self.tasks:
            self.tasks[task_id].merged = True

    # -- queries --------------------------------------------------------------

    def completed_task_ids(self) -> set[str]:
        """Task IDs with status 'done' — used by --only-incomplete to skip."""
        return {tid for tid, rec in self.tasks.items() if rec.status == "done"}

    def is_complete(self) -> bool:
        """True iff every task is done AND merged. Empty task list returns False."""
        if not self.tasks:
            return False
        return all(rec.status == "done" and rec.merged for rec in self.tasks.values())

    def seed_pending(self, task_ids) -> None:
        """Record each task_id with status='pending' if not already present.

        Called at run start so the on-disk status file reflects the full plan
        before any agent has been dispatched. Existing records (carried
        forward from a prior session, or already updated this run) are left
        untouched.
        """
        for tid in task_ids:
            if tid not in self.tasks:
                self.record_task(tid, status="pending")


def iter_status_files(repo: Path) -> list[tuple[Path, dict]]:
    """Return (path, raw_yaml_dict) for every status file, sorted by slug.

    Scans both new (``<plan>/status.yaml``) and legacy (``status-*.yaml``)
    layouts. When both exist for the same plan slug, the new layout wins.
    Returns [] if .workbench/ does not exist.
    """
    wb_dir = repo / ".workbench"
    if not wb_dir.exists():
        return []

    # slug -> (path, data); new layout overwrites legacy
    slug_entries: dict[str, tuple[Path, dict]] = {}

    for path in sorted(wb_dir.glob("status-*.yaml")):
        slug = path.stem.removeprefix("status-")
        data = yaml.safe_load(path.read_text()) or {}
        slug_entries[slug] = (path, data)

    for path in sorted(wb_dir.glob("*/status.yaml")):
        slug = path.parent.name
        data = yaml.safe_load(path.read_text()) or {}
        slug_entries[slug] = (path, data)

    return [(p, d) for p, d in sorted(slug_entries.values(), key=lambda x: x[0])]


def status_file_is_complete(data: dict) -> bool:
    """True iff every session in the file has all tasks done + merged.

    False for: empty/missing sessions block, any session with empty tasks block,
    any task with status != 'done', any task with merged != True.
    """
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not sessions:
        return False
    for session_data in sessions.values():
        tasks = session_data.get("tasks", {}) if isinstance(session_data, dict) else {}
        if not tasks:
            return False
        for rec in tasks.values():
            if not isinstance(rec, dict):
                return False
            if rec.get("status") != "done" or not rec.get("merged"):
                return False
    return True


def status_file_branches(data: dict) -> set[str]:
    """Every branch name recorded across all sessions in the file.

    Records with no branch (or non-string branch) are skipped silently.
    """
    out: set[str] = set()
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not sessions:
        return out
    for session_data in sessions.values():
        if not isinstance(session_data, dict):
            continue
        for rec in session_data.get("tasks", {}).values():
            if not isinstance(rec, dict):
                continue
            branch = rec.get("branch")
            if isinstance(branch, str) and branch:
                out.add(branch)
    return out
