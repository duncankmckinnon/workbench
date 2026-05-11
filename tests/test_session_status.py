"""Tests for session status persistence."""

from __future__ import annotations

import pytest
import yaml

from workbench.session_status import (
    SessionStatus,
    TaskRecord,
    iter_status_files,
    status_file_branches,
    status_file_is_complete,
)


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
        assert (tmp_path / ".workbench" / PLAN_SLUG / "status.yaml").exists()

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

        raw = (tmp_path / ".workbench" / PLAN_SLUG / "status.yaml").read_text()
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

        assert (tmp_path / ".workbench" / "plan-a" / "status.yaml").exists()
        assert (tmp_path / ".workbench" / "plan-b" / "status.yaml").exists()

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


class TestNewLayout:
    """Tests for the per-plan folder layout."""

    def test_save_creates_new_layout(self, tmp_path):
        ss = SessionStatus(plan_slug="my-plan", session_branch=SESSION)
        ss.record_task("task-1", status="done")
        ss.save(tmp_path)
        assert (tmp_path / ".workbench" / "my-plan" / "status.yaml").exists()
        assert not (tmp_path / ".workbench" / "status-my-plan.yaml").exists()

    def test_load_reads_new_layout(self, tmp_path):
        ss = SessionStatus(plan_slug="my-plan", session_branch=SESSION)
        ss.record_task("task-1", status="done", branch="wb/feat")
        ss.save(tmp_path)

        loaded = SessionStatus.load(tmp_path, "my-plan", SESSION)
        assert loaded is not None
        assert loaded.tasks["task-1"].status == "done"

    def test_load_falls_back_to_legacy(self, tmp_path):
        wb = tmp_path / ".workbench"
        wb.mkdir()
        legacy = wb / "status-old-plan.yaml"
        legacy.write_text(
            yaml.dump(
                {
                    "sessions": {
                        SESSION: {"tasks": {"task-1": {"status": "done", "merged": True}}}
                    },
                }
            )
        )

        loaded = SessionStatus.load(tmp_path, "old-plan", SESSION)
        assert loaded is not None
        assert loaded.tasks["task-1"].status == "done"

    def test_load_prefers_new_over_legacy(self, tmp_path):
        wb = tmp_path / ".workbench"
        wb.mkdir()
        # Write legacy
        legacy = wb / "status-my-plan.yaml"
        legacy.write_text(
            yaml.dump(
                {
                    "sessions": {SESSION: {"tasks": {"task-1": {"status": "failed"}}}},
                }
            )
        )
        # Write new layout with different data
        new_dir = wb / "my-plan"
        new_dir.mkdir()
        (new_dir / "status.yaml").write_text(
            yaml.dump(
                {
                    "sessions": {SESSION: {"tasks": {"task-1": {"status": "done"}}}},
                }
            )
        )

        loaded = SessionStatus.load(tmp_path, "my-plan", SESSION)
        assert loaded is not None
        assert loaded.tasks["task-1"].status == "done"


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

    def test_deduplicates_legacy_and_new(self, tmp_path):
        """When both layouts exist for same slug, new layout wins."""
        wb = tmp_path / ".workbench"
        wb.mkdir()
        # Legacy file
        (wb / "status-my-plan.yaml").write_text(
            yaml.dump(
                {
                    "sessions": {"workbench-1": {"tasks": {"task-1": {"status": "failed"}}}},
                }
            )
        )
        # New layout with different data
        plan_dir = wb / "my-plan"
        plan_dir.mkdir()
        (plan_dir / "status.yaml").write_text(
            yaml.dump(
                {
                    "sessions": {"workbench-1": {"tasks": {"task-1": {"status": "done"}}}},
                }
            )
        )

        found = SessionStatus.find_by_session(tmp_path, "workbench-1")
        assert found is not None
        assert found.tasks["task-1"].status == "done"

    def test_finds_session_in_legacy_only(self, tmp_path):
        wb = tmp_path / ".workbench"
        wb.mkdir()
        (wb / "status-old-plan.yaml").write_text(
            yaml.dump(
                {
                    "sessions": {"workbench-1": {"tasks": {"task-1": {"status": "done"}}}},
                }
            )
        )

        found = SessionStatus.find_by_session(tmp_path, "workbench-1")
        assert found is not None
        assert found.plan_slug == "old-plan"


class TestSessionStatusIsComplete:
    def test_empty_tasks_not_complete(self):
        ss = SessionStatus(plan_slug="p", session_branch="workbench-1")
        assert ss.is_complete() is False

    def test_single_done_merged(self):
        ss = SessionStatus(plan_slug="p", session_branch="workbench-1")
        ss.record_task("t1", status="done", merged=True)
        assert ss.is_complete() is True

    def test_all_done_merged(self):
        ss = SessionStatus(plan_slug="p", session_branch="workbench-1")
        ss.record_task("t1", status="done", merged=True)
        ss.record_task("t2", status="done", merged=True)
        assert ss.is_complete() is True

    def test_done_but_not_merged(self):
        ss = SessionStatus(plan_slug="p", session_branch="workbench-1")
        ss.record_task("t1", status="done", merged=False)
        assert ss.is_complete() is False

    def test_failed_task(self):
        ss = SessionStatus(plan_slug="p", session_branch="workbench-1")
        ss.record_task("t1", status="failed", merged=True)
        assert ss.is_complete() is False

    def test_pending_task(self):
        ss = SessionStatus(plan_slug="p", session_branch="workbench-1")
        ss.record_task("t1", status="pending")
        assert ss.is_complete() is False

    def test_one_complete_one_failed(self):
        ss = SessionStatus(plan_slug="p", session_branch="workbench-1")
        ss.record_task("t1", status="done", merged=True)
        ss.record_task("t2", status="failed", merged=False)
        assert ss.is_complete() is False


class TestSeedPending:
    def test_adds_new_tasks_as_pending(self):
        ss = SessionStatus(plan_slug="p", session_branch="workbench-1")
        ss.seed_pending(["task-1", "task-2", "task-3"])

        assert set(ss.tasks.keys()) == {"task-1", "task-2", "task-3"}
        for rec in ss.tasks.values():
            assert rec.status == "pending"
            assert rec.merged is False
            assert rec.branch is None

    def test_preserves_existing_records(self):
        ss = SessionStatus(plan_slug="p", session_branch="workbench-1")
        ss.record_task("task-1", status="done", branch="wb/done", merged=True)
        ss.seed_pending(["task-1", "task-2"])

        # task-1 unchanged; task-2 newly seeded as pending
        assert ss.tasks["task-1"].status == "done"
        assert ss.tasks["task-1"].merged is True
        assert ss.tasks["task-1"].branch == "wb/done"
        assert ss.tasks["task-2"].status == "pending"

    def test_round_trip_with_pending(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        ss = SessionStatus(plan_slug="p", session_branch="workbench-1")
        ss.seed_pending(["task-1", "task-2"])
        ss.save(tmp_path)

        loaded = SessionStatus.load(tmp_path, "p", "workbench-1")
        assert loaded is not None
        assert loaded.tasks["task-1"].status == "pending"
        assert loaded.tasks["task-2"].status == "pending"
        assert loaded.is_complete() is False

    def test_seeded_pending_makes_status_file_incomplete(self, tmp_path):
        """A freshly seeded status file must NOT be classified as complete."""
        (tmp_path / ".workbench").mkdir()
        ss = SessionStatus(plan_slug="p", session_branch="workbench-1")
        ss.seed_pending(["task-1", "task-2"])
        ss.save(tmp_path)

        path = tmp_path / ".workbench" / "p" / "status.yaml"
        data = yaml.safe_load(path.read_text())
        assert status_file_is_complete(data) is False

    def test_idempotent(self):
        ss = SessionStatus(plan_slug="p", session_branch="workbench-1")
        ss.seed_pending(["task-1"])
        ss.seed_pending(["task-1", "task-2"])

        assert set(ss.tasks.keys()) == {"task-1", "task-2"}
        # task-1 was pending after first call and still pending after second
        assert ss.tasks["task-1"].status == "pending"


class TestIterStatusFiles:
    def test_missing_workbench_dir(self, tmp_path):
        assert iter_status_files(tmp_path) == []

    def test_no_status_files(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        assert iter_status_files(tmp_path) == []

    def test_returns_one_per_status_file(self, tmp_path):
        wb = tmp_path / ".workbench"
        wb.mkdir()
        (wb / "status-a.yaml").write_text(yaml.dump({"sessions": {}}))
        (wb / "status-b.yaml").write_text(yaml.dump({"sessions": {"x": {"tasks": {}}}}))

        result = iter_status_files(tmp_path)

        assert len(result) == 2
        # Sorted by path: status-a.yaml comes before status-b.yaml
        assert result[0][0].name == "status-a.yaml"
        assert result[1][0].name == "status-b.yaml"
        assert result[0][1] == {"sessions": {}}
        assert result[1][1] == {"sessions": {"x": {"tasks": {}}}}

    def test_ignores_non_status_files(self, tmp_path):
        wb = tmp_path / ".workbench"
        wb.mkdir()
        (wb / "profile.yaml").write_text(yaml.dump({"roles": {}}))
        (wb / "agents.yaml").write_text(yaml.dump({}))
        (wb / "plans").mkdir()
        (wb / "status-real.yaml").write_text(yaml.dump({"sessions": {}}))

        result = iter_status_files(tmp_path)

        assert len(result) == 1
        assert result[0][0].name == "status-real.yaml"

    def test_handles_empty_yaml(self, tmp_path):
        wb = tmp_path / ".workbench"
        wb.mkdir()
        (wb / "status-empty.yaml").write_text("")

        result = iter_status_files(tmp_path)

        assert len(result) == 1
        assert result[0][1] == {}

    def test_returns_new_layout_files(self, tmp_path):
        wb = tmp_path / ".workbench"
        wb.mkdir()
        plan_dir = wb / "my-plan"
        plan_dir.mkdir()
        (plan_dir / "status.yaml").write_text(yaml.dump({"sessions": {"x": {"tasks": {}}}}))

        result = iter_status_files(tmp_path)
        assert len(result) == 1
        assert result[0][0] == plan_dir / "status.yaml"

    def test_returns_both_layouts_deduplicated(self, tmp_path):
        wb = tmp_path / ".workbench"
        wb.mkdir()
        # Legacy file for plan-a
        (wb / "status-plan-a.yaml").write_text(yaml.dump({"sessions": {"old": {"tasks": {}}}}))
        # New layout for plan-a (should win)
        plan_dir = wb / "plan-a"
        plan_dir.mkdir()
        (plan_dir / "status.yaml").write_text(yaml.dump({"sessions": {"new": {"tasks": {}}}}))
        # Legacy-only plan-b
        (wb / "status-plan-b.yaml").write_text(yaml.dump({"sessions": {}}))

        result = iter_status_files(tmp_path)
        assert len(result) == 2
        slugs = []
        for path, data in result:
            if path.name == "status.yaml":
                slugs.append(path.parent.name)
                # plan-a should come from the new layout
                assert "new" in data.get("sessions", {})
            else:
                slugs.append(path.stem.removeprefix("status-"))
        assert "plan-a" in slugs
        assert "plan-b" in slugs


class TestStatusFileIsComplete:
    def test_empty_dict(self):
        assert status_file_is_complete({}) is False

    def test_no_sessions_key(self):
        assert status_file_is_complete({"plan_source": "/x"}) is False

    def test_empty_sessions(self):
        assert status_file_is_complete({"sessions": {}}) is False

    def test_session_with_empty_tasks(self):
        assert status_file_is_complete({"sessions": {"workbench-1": {"tasks": {}}}}) is False

    def test_one_session_one_complete_task(self):
        data = {
            "sessions": {
                "workbench-1": {
                    "tasks": {"t1": {"status": "done", "merged": True, "branch": "wb/t1"}}
                }
            }
        }
        assert status_file_is_complete(data) is True

    def test_one_task_unmerged(self):
        data = {
            "sessions": {
                "workbench-1": {
                    "tasks": {"t1": {"status": "done", "merged": False, "branch": "wb/t1"}}
                }
            }
        }
        assert status_file_is_complete(data) is False

    def test_two_sessions_one_failing(self):
        data = {
            "sessions": {
                "workbench-1": {"tasks": {"t1": {"status": "done", "merged": True}}},
                "workbench-2": {"tasks": {"t1": {"status": "failed", "merged": False}}},
            }
        }
        assert status_file_is_complete(data) is False

    def test_two_sessions_both_complete(self):
        data = {
            "sessions": {
                "workbench-1": {"tasks": {"t1": {"status": "done", "merged": True}}},
                "workbench-2": {
                    "tasks": {
                        "t1": {"status": "done", "merged": True},
                        "t2": {"status": "done", "merged": True},
                    }
                },
            }
        }
        assert status_file_is_complete(data) is True


class TestStatusFileBranches:
    def test_empty_dict(self):
        assert status_file_branches({}) == set()

    def test_one_session_two_tasks(self):
        data = {
            "sessions": {
                "workbench-1": {
                    "tasks": {
                        "t1": {"status": "done", "merged": True, "branch": "wb/feat-a"},
                        "t2": {"status": "done", "merged": True, "branch": "wb/feat-b"},
                    }
                }
            }
        }
        assert status_file_branches(data) == {"wb/feat-a", "wb/feat-b"}

    def test_two_sessions_shared_branch(self):
        data = {
            "sessions": {
                "workbench-1": {"tasks": {"t1": {"branch": "wb/feat-a"}}},
                "workbench-2": {"tasks": {"t1": {"branch": "wb/feat-a"}}},
            }
        }
        assert status_file_branches(data) == {"wb/feat-a"}

    def test_skips_records_without_branch(self):
        data = {
            "sessions": {
                "workbench-1": {
                    "tasks": {
                        "t1": {"status": "done", "branch": "wb/feat-a"},
                        "t2": {"status": "pending", "branch": None},
                        "t3": {"status": "pending"},
                    }
                }
            }
        }
        assert status_file_branches(data) == {"wb/feat-a"}
