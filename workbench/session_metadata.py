from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class SessionMetadata:
    """Identity of a single agent invocation, for tracing.

    Empty/None fields are omitted from every rendering, so standalone agents
    (no wave/task) and the fully-empty default both degrade gracefully.
    """

    plan: str = ""  # plan folder slug, e.g. "session-trace-metadata"
    wave: int | None = None
    task: str = ""  # bare task id, e.g. "task-3"
    task_title: str = ""  # task slug, e.g. "pipeline-wiring" (prompt block only)
    agent: str = ""  # role value, e.g. "tester"
    step: str = ""  # e.g. "test#1", "review-fix#2", "requirements", "plan"

    def _fields(self) -> list[tuple[str, str]]:
        """Ordered (key, value) pairs for non-empty fields; values stringified."""
        items: list[tuple[str, str]] = []
        if self.plan:
            items.append(("plan", self.plan))
        if self.wave is not None:
            items.append(("wave", str(self.wave)))
        if self.task:
            items.append(("task", self.task))
        if self.agent:
            items.append(("agent", self.agent))
        if self.step:
            items.append(("step", self.step))
        return items

    def render(self) -> str:
        """Fenced ```wb-session block, or '' when no fields are set.

        The `task` line shows 'id (slug)' when task_title is set.
        """
        lines: list[str] = []
        for key, value in self._fields():
            if key == "task" and self.task_title:
                value = f"{self.task} ({self.task_title})"
            lines.append(f"{key}: {value}")
        if not lines:
            return ""
        return "```wb-session\n" + "\n".join(lines) + "\n```"

    def as_env(self) -> dict[str, str]:
        """WB_* vars for non-empty fields plus an OTEL_RESOURCE_ATTRIBUTES string.

        Returns {} when no fields are set. Does NOT merge any inherited
        OTEL_RESOURCE_ATTRIBUTES — that is the job of merge_trace_env().
        """
        fields = self._fields()
        if not fields:
            return {}
        env = {f"WB_{key.upper()}": value for key, value in fields}
        env["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(f"wb.{key}={value}" for key, value in fields)
        return env


def with_session_metadata(prompt: str, meta: SessionMetadata | None) -> str:
    """Prepend the wb-session block to a rendered prompt.

    No-op (returns the prompt unchanged) when meta is None or renders empty.
    """
    block = meta.render() if meta else ""
    return f"{block}\n\n{prompt}" if block else prompt


def merge_trace_env(base: Mapping[str, str], meta: SessionMetadata | None) -> dict[str, str]:
    """Overlay meta.as_env() onto a copy of base.

    OTEL_RESOURCE_ATTRIBUTES is concatenated with any value already present in
    base (``inherited,wb.plan=...,...``) rather than overwritten, so a caller's
    base resource attributes survive. Returns dict(base) unchanged when meta is
    None or empty.
    """
    merged = dict(base)
    if meta is None:
        return merged
    extra = meta.as_env()
    inherited_otel = merged.get("OTEL_RESOURCE_ATTRIBUTES", "")
    for key, value in extra.items():
        if key == "OTEL_RESOURCE_ATTRIBUTES" and inherited_otel:
            merged[key] = f"{inherited_otel},{value}"
        else:
            merged[key] = value
    return merged
