"""Tests for workbench.path_resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from workbench.path_resolver import (
    is_plan_reference_path,
    plan_slug_from_path,
    resolve_agents_config_paths,
    resolve_plan_path,
    resolve_profile_paths,
)


def test_is_plan_reference_path_detects_paths():
    assert is_plan_reference_path("a/b") is True
    assert is_plan_reference_path("x.md") is True
    assert is_plan_reference_path("a\\b") is True
    assert is_plan_reference_path("myname") is False
    assert is_plan_reference_path("foo_bar") is False


def test_resolve_plan_path_new_layout(tmp_path):
    plan_dir = tmp_path / ".workbench" / "foo"
    plan_dir.mkdir(parents=True)
    plan_file = plan_dir / "plan.md"
    plan_file.write_text("# foo")

    result = resolve_plan_path("foo", tmp_path)
    assert result == plan_file


def test_resolve_plan_path_legacy_layout(tmp_path):
    plans_dir = tmp_path / ".workbench" / "plans"
    plans_dir.mkdir(parents=True)
    plan_file = plans_dir / "bar.md"
    plan_file.write_text("# bar")

    result = resolve_plan_path("bar", tmp_path)
    assert result == plan_file


def test_resolve_plan_path_new_wins_over_legacy(tmp_path):
    new_dir = tmp_path / ".workbench" / "baz"
    new_dir.mkdir(parents=True)
    new_file = new_dir / "plan.md"
    new_file.write_text("# baz new")

    legacy_dir = tmp_path / ".workbench" / "plans"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "baz.md"
    legacy_file.write_text("# baz legacy")

    result = resolve_plan_path("baz", tmp_path)
    assert result == new_file


def test_resolve_plan_path_passthrough_for_path_arg(tmp_path):
    result = resolve_plan_path("./somewhere/plan.md", tmp_path)
    assert result == Path("./somewhere/plan.md")


def test_resolve_plan_path_missing_name_raises_filenotfounderror(tmp_path):
    with pytest.raises(FileNotFoundError) as exc_info:
        resolve_plan_path("nonexistent", tmp_path)
    assert "nonexistent" in str(exc_info.value)


def test_plan_slug_from_path_new_layout():
    assert plan_slug_from_path(Path(".workbench/foo/plan.md")) == "foo"


def test_plan_slug_from_path_legacy_layout():
    assert plan_slug_from_path(Path(".workbench/plans/bar.md")) == "bar"


def test_resolve_profile_paths_explicit_wins(tmp_path):
    explicit = tmp_path / "custom_profile.yaml"
    explicit.write_text("dummy")
    result = resolve_profile_paths(tmp_path, plan_slug="foo", explicit_path=explicit)
    assert result == [explicit]


def test_resolve_profile_paths_layered(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    plan_profile = tmp_path / ".workbench" / "foo" / "profile.yaml"
    plan_profile.parent.mkdir(parents=True)
    plan_profile.write_text("plan")

    project_profile = tmp_path / ".workbench" / "profile.yaml"
    project_profile.write_text("project")

    home_profile = fake_home / ".workbench" / "profile.yaml"
    home_profile.parent.mkdir(parents=True)
    home_profile.write_text("home")

    result = resolve_profile_paths(tmp_path, plan_slug="foo")
    assert result == [plan_profile, project_profile, home_profile]


def test_resolve_profile_paths_with_name(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    plan_profile = tmp_path / ".workbench" / "foo" / "profile.fast.yaml"
    plan_profile.parent.mkdir(parents=True)
    plan_profile.write_text("plan-fast")

    project_profile = tmp_path / ".workbench" / "profile.fast.yaml"
    project_profile.write_text("project-fast")

    home_profile = fake_home / ".workbench" / "profile.fast.yaml"
    home_profile.parent.mkdir(parents=True)
    home_profile.write_text("home-fast")

    result = resolve_profile_paths(tmp_path, plan_slug="foo", name="fast")
    assert result == [plan_profile, project_profile, home_profile]


def test_resolve_profile_paths_empty_when_none_exist(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    result = resolve_profile_paths(tmp_path, plan_slug="foo")
    assert result == []


def test_resolve_agents_config_paths_layered(tmp_path):
    plan_agents = tmp_path / ".workbench" / "foo" / "agents.yaml"
    plan_agents.parent.mkdir(parents=True)
    plan_agents.write_text("plan")

    project_agents = tmp_path / ".workbench" / "agents.yaml"
    project_agents.write_text("project")

    result = resolve_agents_config_paths(tmp_path, plan_slug="foo")
    assert result == [plan_agents, project_agents]
