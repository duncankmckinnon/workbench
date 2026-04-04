"""Tests for session status persistence."""

from __future__ import annotations

import json

import pytest

from workbench.session_status import SessionStatus, TaskRecord


class TestTaskRecord:
    def test_round_trip(self):
        rec = TaskRecord(status="done", branch="wb/my-task", merged=True, last_agent="reviewer")
        data = rec.to_dict()
        restored = TaskRecord.from_dict(data)
        assert restored.status == "done"
        assert restored.branch == "wb/my-task"
        assert restored.merged is True
        assert restored.last_agent == "reviewer"

    def test_from_dict_defaults(self):
        rec = TaskRecord.from_dict({})
        assert rec.status == "pending"
        assert rec.branch is None
        assert rec.merged is False
        assert rec.last_agent == ""


class TestSessionStatus:
    def test_save_and_load(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        ss = SessionStatus(session_branch="workbench-1")
        ss.record_task(
            "task-1", status="done", branch="wb/feat-a", merged=True, last_agent="tester"
        )
        ss.record_task("task-2", status="failed", branch="wb/feat-b", last_agent="implementor")
        ss.save(tmp_path)

        loaded = SessionStatus.load(tmp_path)
        assert loaded is not None
        assert loaded.session_branch == "workbench-1"
        assert len(loaded.tasks) == 2
        assert loaded.tasks["task-1"].status == "done"
        assert loaded.tasks["task-1"].merged is True
        assert loaded.tasks["task-2"].status == "failed"
        assert loaded.tasks["task-2"].last_agent == "implementor"

    def test_load_missing_returns_none(self, tmp_path):
        assert SessionStatus.load(tmp_path) is None

    def test_completed_task_ids(self):
        ss = SessionStatus(session_branch="workbench-1")
        ss.record_task("task-1", status="done")
        ss.record_task("task-2", status="failed")
        ss.record_task("task-3", status="done")
        assert ss.completed_task_ids() == {"task-1", "task-3"}

    def test_mark_merged(self):
        ss = SessionStatus(session_branch="workbench-1")
        ss.record_task("task-1", status="done", branch="wb/feat")
        assert ss.tasks["task-1"].merged is False
        ss.mark_merged("task-1")
        assert ss.tasks["task-1"].merged is True

    def test_mark_merged_nonexistent_is_noop(self):
        ss = SessionStatus(session_branch="workbench-1")
        ss.mark_merged("nonexistent")  # should not raise

    def test_save_creates_workbench_dir(self, tmp_path):
        ss = SessionStatus(session_branch="workbench-1")
        ss.record_task("task-1", status="done")
        ss.save(tmp_path)
        assert (tmp_path / ".workbench" / "status.json").exists()

    def test_record_task_overwrites(self):
        ss = SessionStatus(session_branch="workbench-1")
        ss.record_task("task-1", status="failed", last_agent="implementor")
        ss.record_task("task-1", status="done", last_agent="tester")
        assert ss.tasks["task-1"].status == "done"
        assert ss.tasks["task-1"].last_agent == "tester"

    def test_status_json_is_valid_json(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        ss = SessionStatus(session_branch="workbench-1")
        ss.record_task("task-1", status="done", branch="wb/feat")
        ss.save(tmp_path)

        raw = (tmp_path / ".workbench" / "status.json").read_text()
        data = json.loads(raw)
        assert data["session_branch"] == "workbench-1"
        assert "task-1" in data["tasks"]
