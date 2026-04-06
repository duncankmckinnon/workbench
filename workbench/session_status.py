"""Persistent session status tracking.

Writes per-task status to ``.workbench/status-<plan>.yaml`` so that
``--only-failed`` can reliably skip already-completed tasks,
even when the process was interrupted mid-wave.

Files are named by plan slug. Inside each file, sessions are keyed
by session branch, so multiple runs of the same plan coexist.
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

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "branch": self.branch,
            "merged": self.merged,
            "last_agent": self.last_agent,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TaskRecord:
        return cls(
            status=data.get("status", "pending"),
            branch=data.get("branch"),
            merged=data.get("merged", False),
            last_agent=data.get("last_agent", ""),
        )


@dataclass
class SessionStatus:
    """Read/write session status to disk.

    File: ``.workbench/status-<plan_slug>.yaml``
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
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # -- persistence ----------------------------------------------------------

    @staticmethod
    def path_for(repo: Path, plan_slug: str) -> Path:
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
        sessions[self.session_branch] = {
            "tasks": {tid: rec.to_dict() for tid, rec in self.tasks.items()},
        }

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
    ) -> None:
        """Atomically record a task and save to disk under a lock."""
        async with self._lock:
            self.record_task(task_id, status, branch, merged, last_agent)
            self.save(repo)

    async def update_merged(self, repo: Path, task_id: str) -> None:
        """Atomically mark a task merged and save under a lock."""
        async with self._lock:
            self.mark_merged(task_id)
            self.save(repo)

    @classmethod
    def load(cls, repo: Path, plan_slug: str, session_branch: str) -> SessionStatus | None:
        """Load status for a specific plan and session branch.

        Returns None if the file doesn't exist or the session branch
        is not found in the file.
        """
        path = cls.path_for(repo, plan_slug)
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
        return cls(
            plan_slug=plan_slug,
            session_branch=session_branch,
            plan_source=data.get("plan_source", ""),
            tasks=tasks,
        )

    @classmethod
    def find_by_session(cls, repo: Path, session_branch: str) -> SessionStatus | None:
        """Find a session across all status files.

        Scans ``.workbench/status-*.yaml`` for one containing the given
        session branch. Returns the matching ``SessionStatus``, or None
        if no match is found. Raises ``ValueError`` if multiple status
        files contain the same session branch.
        """
        wb_dir = repo / ".workbench"
        if not wb_dir.exists():
            return None

        matches: list[SessionStatus] = []
        for path in sorted(wb_dir.glob("status-*.yaml")):
            # Extract plan slug from filename: status-<slug>.yaml
            slug = path.stem.removeprefix("status-")
            data = yaml.safe_load(path.read_text()) or {}
            sessions = data.get("sessions", {})
            if session_branch in sessions:
                session_data = sessions[session_branch]
                tasks = {
                    tid: TaskRecord.from_dict(rec)
                    for tid, rec in session_data.get("tasks", {}).items()
                }
                matches.append(
                    cls(
                        plan_slug=slug,
                        session_branch=session_branch,
                        plan_source=data.get("plan_source", ""),
                        tasks=tasks,
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
    ) -> None:
        self.tasks[task_id] = TaskRecord(
            status=status,
            branch=branch,
            merged=merged,
            last_agent=last_agent,
        )

    def mark_merged(self, task_id: str) -> None:
        if task_id in self.tasks:
            self.tasks[task_id].merged = True

    # -- queries --------------------------------------------------------------

    def completed_task_ids(self) -> set[str]:
        """Task IDs with status 'done' — used by --only-failed to skip."""
        return {tid for tid, rec in self.tasks.items() if rec.status == "done"}
