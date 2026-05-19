from __future__ import annotations

from pathlib import Path

from workbench.conventions import (
    TEMPLATE,
    apply_conventions_fallback,
    conventions_path,
    load_conventions_text,
)


class TestConventionsPath:
    def test_returns_workbench_conventions_md(self, tmp_path):
        assert conventions_path(tmp_path) == tmp_path / ".workbench" / "conventions.md"


class TestLoadConventionsText:
    def test_returns_empty_when_file_missing(self, tmp_path):
        assert load_conventions_text(tmp_path) == ""

    def test_returns_stripped_contents(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        (tmp_path / ".workbench" / "conventions.md").write_text("\n\n# Hi\n\n")
        assert load_conventions_text(tmp_path) == "# Hi"

    def test_empty_file_returns_empty_string(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        (tmp_path / ".workbench" / "conventions.md").write_text("   \n\t\n")
        assert load_conventions_text(tmp_path) == ""


class TestApplyConventionsFallback:
    def test_no_file_returns_unchanged(self, tmp_path):
        plan = "# Plan\n\n## Context\n\nstuff\n"
        assert apply_conventions_fallback(plan, tmp_path) == plan

    def test_empty_file_returns_unchanged(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        (tmp_path / ".workbench" / "conventions.md").write_text("")
        plan = "# Plan\n"
        assert apply_conventions_fallback(plan, tmp_path) == plan

    def test_plan_with_conventions_section_returns_unchanged(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        (tmp_path / ".workbench" / "conventions.md").write_text("# Project\n- rule\n")
        plan = "# Plan\n\n## Conventions\n\n- own rule\n\n## Task: a\n"
        assert apply_conventions_fallback(plan, tmp_path) == plan

    def test_plan_with_conventions_heading_case_insensitive(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        (tmp_path / ".workbench" / "conventions.md").write_text("file\n")
        plan = "# Plan\n\n## CONVENTIONS\n\n- own\n"
        assert apply_conventions_fallback(plan, tmp_path) == plan

    def test_appends_section_when_file_present_and_plan_lacks_one(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        (tmp_path / ".workbench" / "conventions.md").write_text("- rule a\n- rule b\n")
        plan = "# Plan\n\n## Context\n\nstuff\n"
        out = apply_conventions_fallback(plan, tmp_path)
        assert out.endswith("## Conventions\n\n- rule a\n- rule b\n")
        assert "## Context\n\nstuff" in out

    def test_idempotent(self, tmp_path):
        (tmp_path / ".workbench").mkdir()
        (tmp_path / ".workbench" / "conventions.md").write_text("- rule\n")
        plan = "# Plan\n"
        once = apply_conventions_fallback(plan, tmp_path)
        twice = apply_conventions_fallback(once, tmp_path)
        assert once == twice


class TestTemplate:
    def test_template_has_required_sections(self):
        for heading in ("## Code style", "## Testing", "## Git", "## Naming"):
            assert heading in TEMPLATE
