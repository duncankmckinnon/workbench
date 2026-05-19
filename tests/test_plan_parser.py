"""Tests for the plan parser module."""

import pytest

from workbench.plan_parser import parse_plan


def test_parse_single_task(tmp_path):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "# My Plan\n\n" "## Task: Build the widget\n" "Implement the widget component.\n"
    )
    plan = parse_plan(plan_file)
    assert len(plan.tasks) == 1
    task = plan.tasks[0]
    assert task.id == "task-1"
    assert task.title == "Build the widget"
    assert "Implement the widget component." in task.description


def test_parse_multiple_tasks(tmp_path):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "# Plan\n\n"
        "## Task: First thing\n"
        "Do first.\n\n"
        "## Task: Second thing\n"
        "Do second.\n"
    )
    plan = parse_plan(plan_file)
    assert len(plan.tasks) == 2
    assert plan.tasks[0].id == "task-1"
    assert plan.tasks[1].id == "task-2"


def test_parse_files_metadata(tmp_path):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "# Plan\n\n"
        "## Task: Update models\n"
        "Files: models.py, views.py, tests.py\n"
        "Change the models.\n"
    )
    plan = parse_plan(plan_file)
    assert plan.tasks[0].files == ["models.py", "views.py", "tests.py"]


def test_parse_depends_metadata(tmp_path):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "# Plan\n\n"
        "## Task: Setup base\n"
        "Create the base.\n\n"
        "## Task: Add feature\n"
        "Depends: setup-base\n"
        "Build on the base.\n"
    )
    plan = parse_plan(plan_file)
    # "setup-base" slug should resolve to "task-1"
    assert plan.tasks[1].depends_on == ["task-1"]


def test_independent_groups_no_deps(tmp_path):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "# Plan\n\n" "## Task: A\nDo A.\n\n" "## Task: B\nDo B.\n\n" "## Task: C\nDo C.\n"
    )
    plan = parse_plan(plan_file)
    groups = plan.independent_groups
    assert len(groups) == 1
    assert len(groups[0]) == 3


def test_independent_groups_linear_deps(tmp_path):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "# Plan\n\n"
        "## Task: A\nDo A.\n\n"
        "## Task: B\nDepends: a\nDo B.\n\n"
        "## Task: C\nDepends: b\nDo C.\n"
    )
    plan = parse_plan(plan_file)
    groups = plan.independent_groups
    # A -> B -> C: three separate waves
    assert len(groups) == 3
    assert groups[0][0].title == "A"
    assert groups[1][0].title == "B"
    assert groups[2][0].title == "C"


def test_independent_groups_diamond(tmp_path):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "# Plan\n\n"
        "## Task: A\nDo A.\n\n"
        "## Task: B\nDepends: a\nDo B.\n\n"
        "## Task: C\nDepends: a\nDo C.\n\n"
        "## Task: D\nDepends: b, c\nDo D.\n"
    )
    plan = parse_plan(plan_file)
    groups = plan.independent_groups
    # Wave 0: A, Wave 1: B+C, Wave 2: D
    assert len(groups) == 3
    assert [t.title for t in groups[0]] == ["A"]
    assert sorted(t.title for t in groups[1]) == ["B", "C"]
    assert [t.title for t in groups[2]] == ["D"]


def test_empty_plan(tmp_path):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Empty Plan\n\nNo tasks here.\n")
    plan = parse_plan(plan_file)
    assert plan.tasks == []


def test_plan_title(tmp_path):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# My Great Plan\n\n## Task: X\nDo X.\n")
    plan = parse_plan(plan_file)
    assert plan.title == "My Great Plan"


def test_folder_id_returns_parent_name(tmp_path):
    plan_dir = tmp_path / "my-project"
    plan_dir.mkdir()
    plan_file = plan_dir / "plan.md"
    plan_file.write_text("# My Great Plan\n\n## Task: X\nDo X.\n")
    plan = parse_plan(plan_file)
    assert plan.folder_id == "my-project"


def test_folder_id_legacy_layout_returns_file_stem(tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_file = plans_dir / "my-feature.md"
    plan_file.write_text("# My Feature\n\n## Task: X\nDo X.\n")
    plan = parse_plan(plan_file)
    assert plan.folder_id == "my-feature"


def test_task_slug(tmp_path):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n\n" "## Task: Add User Authentication\n" "Implement auth.\n")
    plan = parse_plan(plan_file)
    assert plan.tasks[0].slug == "add-user-authentication"


class TestRunConfigFrontmatter:
    def test_no_frontmatter_returns_empty_dict(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# My Plan\n\n## Task: Do stuff\nDo it.\n")
        plan = parse_plan(plan_file)
        assert plan.run_config == {}

    def test_empty_frontmatter(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("---\n---\n# Title\n## Task: x\nDo it.\n")
        plan = parse_plan(plan_file)
        assert plan.run_config == {}
        assert len(plan.tasks) == 1

    def test_simple_string_field(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text(
            "---\nsession_branch: workbench-feat\n---\n# Plan\n## Task: x\nDo it.\n"
        )
        plan = parse_plan(plan_file)
        assert plan.run_config["session_branch"] == "workbench-feat"

    def test_bool_field(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("---\ntdd: true\n---\n# Plan\n## Task: x\nDo it.\n")
        plan = parse_plan(plan_file)
        assert plan.run_config["tdd"] is True

    def test_int_field(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("---\nmax_concurrent: 8\n---\n# Plan\n## Task: x\nDo it.\n")
        plan = parse_plan(plan_file)
        assert plan.run_config["max_concurrent"] == 8

    def test_multiple_fields(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text(
            "---\n"
            "session_branch: wb-auth\n"
            "base: feature-auth\n"
            "tdd: true\n"
            "agent: claude\n"
            "max_concurrent: 6\n"
            "profile_name: fast\n"
            "---\n"
            "# Auth refactor\n\n"
            "## Context\n\nSome context here.\n\n"
            "## Task: First\nDo first.\n\n"
            "## Task: Second\nDo second.\n"
        )
        plan = parse_plan(plan_file)
        assert plan.run_config["session_branch"] == "wb-auth"
        assert plan.run_config["base"] == "feature-auth"
        assert plan.run_config["tdd"] is True
        assert plan.run_config["agent"] == "claude"
        assert plan.run_config["max_concurrent"] == 6
        assert plan.run_config["profile_name"] == "fast"
        assert plan.title == "Auth refactor"
        assert "Some context here." in plan.context
        assert len(plan.tasks) == 2

    def test_unknown_key_errors(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("---\nbogus: true\n---\n# Plan\n## Task: x\nDo it.\n")
        with pytest.raises(ValueError, match=r"Unknown.*bogus"):
            parse_plan(plan_file)

    def test_wrong_type_string_for_bool(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text('---\ntdd: "yes"\n---\n# Plan\n## Task: x\nDo it.\n')
        with pytest.raises(ValueError, match=r"tdd.*bool"):
            parse_plan(plan_file)

    def test_wrong_type_int_as_bool(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("---\ntdd: 1\n---\n# Plan\n## Task: x\nDo it.\n")
        with pytest.raises(ValueError, match=r"tdd.*bool"):
            parse_plan(plan_file)

    def test_wrong_type_bool_as_int(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("---\nmax_concurrent: true\n---\n# Plan\n## Task: x\nDo it.\n")
        with pytest.raises(ValueError, match=r"max_concurrent.*int"):
            parse_plan(plan_file)

    def test_max_concurrent_zero_errors(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("---\nmax_concurrent: 0\n---\n# Plan\n## Task: x\nDo it.\n")
        with pytest.raises(ValueError, match=r">= 1"):
            parse_plan(plan_file)

    def test_max_concurrent_negative_errors(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("---\nmax_concurrent: -1\n---\n# Plan\n## Task: x\nDo it.\n")
        with pytest.raises(ValueError, match=r">= 1"):
            parse_plan(plan_file)

    def test_max_retries_negative_errors(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("---\nmax_retries: -1\n---\n# Plan\n## Task: x\nDo it.\n")
        with pytest.raises(ValueError, match=r">= 0"):
            parse_plan(plan_file)

    def test_max_retries_zero_ok(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("---\nmax_retries: 0\n---\n# Plan\n## Task: x\nDo it.\n")
        plan = parse_plan(plan_file)
        assert plan.run_config["max_retries"] == 0

    def test_non_mapping_frontmatter_errors(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("---\n- item\n---\n# Title\n## Task: x\nDo.\n")
        with pytest.raises(ValueError, match=r"must be a YAML mapping"):
            parse_plan(plan_file)

    def test_frontmatter_not_at_top_is_ignored(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Title\n\n---\ntdd: true\n---\n\n## Task: x\nDo it.\n")
        plan = parse_plan(plan_file)
        assert plan.run_config == {}

    def test_existing_plan_format_still_works(self, tmp_path):
        plan_file = tmp_path / "plan.md"
        plan_file.write_text(
            "# Directive Refactor\n\n"
            "## Context\n\n"
            "Refactor directive handling for clarity.\n\n"
            "## Conventions\n\n"
            "- Python 3.11+\n"
            "- Use dataclasses\n\n"
            "## Task: Extract directives\n"
            "Files: workbench/directives.py\n"
            "Move directive text to separate markdown files.\n\n"
            "## Task: Update references\n"
            "Files: workbench/cli.py, workbench/orchestrator.py\n"
            "Depends: extract-directives\n"
            "Update all imports and references.\n"
        )
        plan = parse_plan(plan_file)
        assert plan.title == "Directive Refactor"
        assert len(plan.tasks) == 2
        assert plan.run_config == {}
        assert plan.tasks[0].files == ["workbench/directives.py"]
        assert plan.tasks[1].depends_on == ["task-1"]


def test_parse_plan_without_repo_arg_does_not_apply_fallback(tmp_path):
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n\n## Task: t\nDo it.\n")
    wb = tmp_path / ".workbench"
    wb.mkdir()
    (wb / "conventions.md").write_text("- rule\n")

    plan = parse_plan(plan_path)
    assert plan.conventions == ""


def test_parse_plan_with_repo_applies_fallback_when_section_missing(tmp_path):
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n\n## Task: t\nDo it.\n")
    wb = tmp_path / ".workbench"
    wb.mkdir()
    (wb / "conventions.md").write_text("- shared rule\n")

    plan = parse_plan(plan_path, repo=tmp_path)
    assert "shared rule" in plan.conventions


def test_parse_plan_with_repo_skips_fallback_when_section_present(tmp_path):
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n\n## Conventions\n\n- own rule\n\n## Task: t\nDo it.\n")
    wb = tmp_path / ".workbench"
    wb.mkdir()
    (wb / "conventions.md").write_text("- file rule\n")

    plan = parse_plan(plan_path, repo=tmp_path)
    assert "own rule" in plan.conventions
    assert "file rule" not in plan.conventions


def test_parse_plan_with_repo_and_no_file_returns_empty_conventions(tmp_path):
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n\n## Task: t\nDo it.\n")

    plan = parse_plan(plan_path, repo=tmp_path)
    assert plan.conventions == ""
