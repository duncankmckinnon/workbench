"""Integration-style tests for the per-plan folder layout migration."""

from __future__ import annotations

import yaml

from workbench.session_status import SessionStatus

SESSION = "workbench-1"


class TestLayoutMigration:
    def test_load_from_legacy_file(self, tmp_path):
        """SessionStatus.load reads from a legacy status-<slug>.yaml file."""
        wb = tmp_path / ".workbench"
        wb.mkdir()
        (wb / "status-foo.yaml").write_text(
            yaml.dump(
                {
                    "plan_source": "/path/to/foo.md",
                    "sessions": {
                        SESSION: {
                            "tasks": {
                                "task-1": {"status": "done", "branch": "wb/t1", "merged": True},
                                "task-2": {"status": "failed", "branch": "wb/t2", "merged": False},
                            }
                        }
                    },
                }
            )
        )

        loaded = SessionStatus.load(tmp_path, "foo", SESSION)
        assert loaded is not None
        assert loaded.plan_slug == "foo"
        assert loaded.plan_source == "/path/to/foo.md"
        assert loaded.tasks["task-1"].status == "done"
        assert loaded.tasks["task-2"].status == "failed"

    def test_save_writes_new_layout_legacy_untouched(self, tmp_path):
        """After save(), new file exists at foo/status.yaml; legacy is untouched."""
        wb = tmp_path / ".workbench"
        wb.mkdir()
        legacy_path = wb / "status-foo.yaml"
        legacy_content = yaml.dump(
            {
                "sessions": {SESSION: {"tasks": {"task-1": {"status": "done", "merged": True}}}},
            }
        )
        legacy_path.write_text(legacy_content)

        # Load from legacy, then save (writes to new layout)
        loaded = SessionStatus.load(tmp_path, "foo", SESSION)
        assert loaded is not None
        loaded.save(tmp_path)

        # New layout file exists
        new_path = wb / "foo" / "status.yaml"
        assert new_path.exists()

        # Legacy file still exists and is untouched
        assert legacy_path.exists()
        assert legacy_path.read_text() == legacy_content

    def test_subsequent_load_reads_new_file(self, tmp_path):
        """After save(), load() reads from the new layout file."""
        wb = tmp_path / ".workbench"
        wb.mkdir()
        (wb / "status-foo.yaml").write_text(
            yaml.dump(
                {
                    "sessions": {SESSION: {"tasks": {"task-1": {"status": "failed"}}}},
                }
            )
        )

        # Load from legacy
        loaded = SessionStatus.load(tmp_path, "foo", SESSION)
        assert loaded is not None

        # Update and save to new layout
        loaded.record_task("task-1", status="done", merged=True)
        loaded.save(tmp_path)

        # Subsequent load should read from new layout (with updated data)
        reloaded = SessionStatus.load(tmp_path, "foo", SESSION)
        assert reloaded is not None
        assert reloaded.tasks["task-1"].status == "done"
        assert reloaded.tasks["task-1"].merged is True
