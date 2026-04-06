"""Tests for session status persistence."""

from __future__ import annotations

import pytest
import yaml

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


PLAN_SLUG = "test-plan"
SESSION = "workbench-1"


class TestSessionStatus:
    def test_save_and_load(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        ss = SessionStatus(plan_slug=PLAN_SLUG, session_branch=SESSION)
        ss.record_task(
            "task-1", status="done", branch="wb/feat-a", merged=True, last_agent="tester"
        )
        ss.record_task("task-2", status="failed", branch="wb/feat-b", last_agent="implementor")
        ss.save(tmp_path)

        loaded = SessionStatus.load(tmp_path, PLAN_SLUG, SESSION)
        assert loaded is not None
        assert loaded.session_branch == SESSION
        assert loaded.plan_slug == PLAN_SLUG
        assert len(loaded.tasks) == 2
        assert loaded.tasks["task-1"].status == "done"
        assert loaded.tasks["task-1"].merged is True
        assert loaded.tasks["task-2"].status == "failed"
        assert loaded.tasks["task-2"].last_agent == "implementor"

    def test_load_missing_returns_none(self, tmp_path):
        assert SessionStatus.load(tmp_path, PLAN_SLUG, SESSION) is None

    def test_load_wrong_session_returns_none(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        ss = SessionStatus(plan_slug=PLAN_SLUG, session_branch=SESSION)
        ss.record_task("task-1", status="done")
        ss.save(tmp_path)

        assert SessionStatus.load(tmp_path, PLAN_SLUG, "other-session") is None

    def test_completed_task_ids(self):
        ss = SessionStatus(plan_slug=PLAN_SLUG, session_branch=SESSION)
        ss.record_task("task-1", status="done")
        ss.record_task("task-2", status="failed")
        ss.record_task("task-3", status="done")
        assert ss.completed_task_ids() == {"task-1", "task-3"}

    def test_mark_merged(self):
        ss = SessionStatus(plan_slug=PLAN_SLUG, session_branch=SESSION)
        ss.record_task("task-1", status="done", branch="wb/feat")
        assert ss.tasks["task-1"].merged is False
        ss.mark_merged("task-1")
        assert ss.tasks["task-1"].merged is True

    def test_mark_merged_nonexistent_is_noop(self):
        ss = SessionStatus(plan_slug=PLAN_SLUG, session_branch=SESSION)
        ss.mark_merged("nonexistent")  # should not raise

    def test_save_creates_workbench_dir(self, tmp_path):
        ss = SessionStatus(plan_slug=PLAN_SLUG, session_branch=SESSION)
        ss.record_task("task-1", status="done")
        ss.save(tmp_path)
        assert (tmp_path / ".workbench" / f"status-{PLAN_SLUG}.yaml").exists()

    def test_record_task_overwrites(self):
        ss = SessionStatus(plan_slug=PLAN_SLUG, session_branch=SESSION)
        ss.record_task("task-1", status="failed", last_agent="implementor")
        ss.record_task("task-1", status="done", last_agent="tester")
        assert ss.tasks["task-1"].status == "done"
        assert ss.tasks["task-1"].last_agent == "tester"

    def test_status_file_is_valid_yaml(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        ss = SessionStatus(plan_slug=PLAN_SLUG, session_branch=SESSION)
        ss.record_task("task-1", status="done", branch="wb/feat")
        ss.save(tmp_path)

        raw = (tmp_path / ".workbench" / f"status-{PLAN_SLUG}.yaml").read_text()
        data = yaml.safe_load(raw)
        assert SESSION in data["sessions"]
        assert "task-1" in data["sessions"][SESSION]["tasks"]

    def test_multiple_sessions_in_same_file(self, tmp_path):
        (tmp_path / ".workbench").mkdir()

        ss1 = SessionStatus(plan_slug=PLAN_SLUG, session_branch="workbench-1")
        ss1.record_task("task-1", status="done")
        ss1.save(tmp_path)

        ss2 = SessionStatus(plan_slug=PLAN_SLUG, session_branch="workbench-2")
        ss2.record_task("task-1", status="failed")
        ss2.save(tmp_path)

        loaded1 = SessionStatus.load(tmp_path, PLAN_SLUG, "workbench-1")
        loaded2 = SessionStatus.load(tmp_path, PLAN_SLUG, "workbench-2")
        assert loaded1.tasks["task-1"].status == "done"
        assert loaded2.tasks["task-1"].status == "failed"

    def test_different_plans_use_different_files(self, tmp_path):
        (tmp_path / ".workbench").mkdir()

        ss1 = SessionStatus(plan_slug="plan-a", session_branch=SESSION)
        ss1.record_task("task-1", status="done")
        ss1.save(tmp_path)

        ss2 = SessionStatus(plan_slug="plan-b", session_branch=SESSION)
        ss2.record_task("task-1", status="failed")
        ss2.save(tmp_path)

        assert (tmp_path / ".workbench" / "status-plan-a.yaml").exists()
        assert (tmp_path / ".workbench" / "status-plan-b.yaml").exists()

        loaded_a = SessionStatus.load(tmp_path, "plan-a", SESSION)
        loaded_b = SessionStatus.load(tmp_path, "plan-b", SESSION)
        assert loaded_a.tasks["task-1"].status == "done"
        assert loaded_b.tasks["task-1"].status == "failed"

    def test_plan_source_persisted(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        ss = SessionStatus(
            plan_slug=PLAN_SLUG, session_branch=SESSION, plan_source="/path/to/plan.md"
        )
        ss.record_task("task-1", status="done")
        ss.save(tmp_path)

        loaded = SessionStatus.load(tmp_path, PLAN_SLUG, SESSION)
        assert loaded.plan_source == "/path/to/plan.md"


class TestFindBySession:
    def test_finds_unique_match(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        ss = SessionStatus(plan_slug="my-plan", session_branch="workbench-1")
        ss.record_task("task-1", status="done")
        ss.save(tmp_path)

        found = SessionStatus.find_by_session(tmp_path, "workbench-1")
        assert found is not None
        assert found.plan_slug == "my-plan"
        assert found.tasks["task-1"].status == "done"

    def test_returns_none_when_no_match(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        ss = SessionStatus(plan_slug="my-plan", session_branch="workbench-1")
        ss.record_task("task-1", status="done")
        ss.save(tmp_path)

        assert SessionStatus.find_by_session(tmp_path, "workbench-99") is None

    def test_returns_none_when_no_files(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        assert SessionStatus.find_by_session(tmp_path, "workbench-1") is None

    def test_returns_none_when_no_workbench_dir(self, tmp_path):
        assert SessionStatus.find_by_session(tmp_path, "workbench-1") is None

    def test_raises_on_ambiguous_match(self, tmp_path):
        (tmp_path / ".workbench").mkdir()

        ss1 = SessionStatus(plan_slug="plan-a", session_branch="workbench-1")
        ss1.record_task("task-1", status="done")
        ss1.save(tmp_path)

        ss2 = SessionStatus(plan_slug="plan-b", session_branch="workbench-1")
        ss2.record_task("task-1", status="failed")
        ss2.save(tmp_path)

        with pytest.raises(ValueError, match="multiple status files"):
            SessionStatus.find_by_session(tmp_path, "workbench-1")

    def test_ignores_other_sessions_in_same_file(self, tmp_path):
        (tmp_path / ".workbench").mkdir()

        ss1 = SessionStatus(plan_slug="my-plan", session_branch="workbench-1")
        ss1.record_task("task-1", status="done")
        ss1.save(tmp_path)

        ss2 = SessionStatus(plan_slug="my-plan", session_branch="workbench-2")
        ss2.record_task("task-1", status="failed")
        ss2.save(tmp_path)

        found = SessionStatus.find_by_session(tmp_path, "workbench-2")
        assert found is not None
        assert found.session_branch == "workbench-2"
        assert found.tasks["task-1"].status == "failed"
