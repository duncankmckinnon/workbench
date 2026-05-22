from __future__ import annotations

import pytest

from workbench.session_metadata import SessionMetadata, merge_trace_env, with_session_metadata


@pytest.fixture
def meta_full() -> SessionMetadata:
    return SessionMetadata(
        plan="p",
        wave=2,
        task="task-3",
        task_title="pipeline-wiring",
        agent="tester",
        step="test#1",
    )


class TestRender:
    def test_full_block(self, meta_full):
        out = meta_full.render()
        assert out.startswith("```wb-session")
        assert out.endswith("```")
        assert "plan: p" in out
        assert "wave: 2" in out
        assert "task: task-3 (pipeline-wiring)" in out
        assert "agent: tester" in out
        assert "step: test#1" in out

    def test_omits_empty_fields(self):
        meta = SessionMetadata(plan="p", agent="planner", step="plan")
        out = meta.render()
        assert "plan: p" in out
        assert "agent: planner" in out
        assert "step: plan" in out
        assert "wave:" not in out
        assert "task:" not in out

    def test_all_default_is_empty(self):
        assert SessionMetadata().render() == ""


class TestAsEnv:
    def test_full_object(self, meta_full):
        assert meta_full.as_env() == {
            "WB_PLAN": "p",
            "WB_WAVE": "2",
            "WB_TASK": "task-3",
            "WB_AGENT": "tester",
            "WB_STEP": "test#1",
            "OTEL_RESOURCE_ATTRIBUTES": (
                "wb.plan=p,wb.wave=2,wb.task=task-3,wb.agent=tester,wb.step=test#1"
            ),
        }

    def test_empty_metadata(self):
        assert SessionMetadata().as_env() == {}


class TestWithSessionMetadata:
    def test_prepends_block(self, meta_full):
        result = with_session_metadata("BODY", meta_full)
        assert result == f"{meta_full.render()}\n\nBODY"

    def test_none_returns_prompt_unchanged(self):
        assert with_session_metadata("BODY", None) == "BODY"

    def test_empty_metadata_returns_prompt_unchanged(self):
        assert with_session_metadata("BODY", SessionMetadata()) == "BODY"


class TestMergeTraceEnv:
    def test_overlays_onto_base(self, meta_full):
        merged = merge_trace_env({"PATH": "/x"}, meta_full)
        assert merged["PATH"] == "/x"
        assert merged["WB_PLAN"] == "p"
        assert merged["WB_WAVE"] == "2"
        assert merged["WB_TASK"] == "task-3"
        assert merged["WB_AGENT"] == "tester"
        assert merged["WB_STEP"] == "test#1"
        assert "OTEL_RESOURCE_ATTRIBUTES" in merged

    def test_inherits_otel_resource_attributes(self, meta_full):
        merged = merge_trace_env({"OTEL_RESOURCE_ATTRIBUTES": "service.name=foo"}, meta_full)
        assert merged["OTEL_RESOURCE_ATTRIBUTES"].startswith("service.name=foo,wb.plan=p")

    def test_none_returns_base_copy(self):
        assert merge_trace_env({"A": "1"}, None) == {"A": "1"}
