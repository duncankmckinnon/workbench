"""Persistent session status tracking.

Writes per-task status to `.workbench/status.json` so that
``--only-failed`` can reliably skip already-completed tasks,
even when the process was interrupted mid-wave.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path


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
    """Read/write session status to disk."""

    session_branch: str
    tasks: dict[str, TaskRecord] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # -- persistence ----------------------------------------------------------

    @staticmethod
    def path_for(repo: Path) -> Path:
        return repo / ".workbench" / "status.json"

    def save(self, repo: Path) -> None:
        """Synchronous save — call from non-async code or when lock is already held."""
        path = self.path_for(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "session_branch": self.session_branch,
            "tasks": {tid: rec.to_dict() for tid, rec in self.tasks.items()},
        }
        path.write_text(json.dumps(data, indent=2) + "\n")

    async def update_task(
        self,
        repo: Path,
        task_id: str,
        status: str,
        branch: str | None = None,
        merged: bool = False,
        last_agent: str = "",
    ) -> None:
        """Atomically record a task and save to disk under a lock.

        Use this from concurrent coroutines (e.g. inside _run_task)
        to prevent interleaving between record + save.
        """
        async with self._lock:
            self.record_task(task_id, status, branch, merged, last_agent)
            self.save(repo)

    async def update_merged(self, repo: Path, task_id: str) -> None:
        """Atomically mark a task merged and save under a lock."""
        async with self._lock:
            self.mark_merged(task_id)
            self.save(repo)

    @classmethod
    def load(cls, repo: Path) -> SessionStatus | None:
        path = cls.path_for(repo)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        tasks = {tid: TaskRecord.from_dict(rec) for tid, rec in data.get("tasks", {}).items()}
        return cls(session_branch=data["session_branch"], tasks=tasks)

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
