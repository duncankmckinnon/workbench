"""Tests for the plan parser module."""

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


def test_task_slug(tmp_path):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n\n" "## Task: Add User Authentication\n" "Implement auth.\n")
    plan = parse_plan(plan_file)
    assert plan.tasks[0].slug == "add-user-authentication"
