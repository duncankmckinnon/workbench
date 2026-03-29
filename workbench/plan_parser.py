"""Parse a markdown plan into independent tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Task:
    """A single unit of work parsed from a plan."""
    id: str
    title: str
    description: str
    files: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")


@dataclass
class Plan:
    """A parsed plan containing tasks."""
    title: str
    tasks: list[Task]
    source: Path
    context: str = ""
    conventions: str = ""

    @property
    def independent_groups(self) -> list[list[Task]]:
        """Group tasks into waves based on dependencies.

        Wave 0: tasks with no dependencies
        Wave 1: tasks depending only on wave 0, etc.
        """
        resolved: set[str] = set()
        waves: list[list[Task]] = []
        remaining = list(self.tasks)

        while remaining:
            wave = [t for t in remaining if all(d in resolved for d in t.depends_on)]
            if not wave:
                # Circular dependency or unresolvable - dump the rest
                waves.append(remaining)
                break
            waves.append(wave)
            resolved.update(t.id for t in wave)
            remaining = [t for t in remaining if t.id not in resolved]

        return waves


def parse_plan(path: Path) -> Plan:
    """Parse a markdown plan file into a Plan object.

    Expected format:

    # Plan Title

    ## Task: <title>
    Files: file1.py, file2.py
    Depends: task-1, task-2

    Description of what to do...

    ## Task: <title>
    ...
    """
    text = path.read_text()
    lines = text.split("\n")

    # Extract plan title
    plan_title = "Untitled Plan"
    for line in lines:
        if line.startswith("# "):
            plan_title = line[2:].strip()
            break

    # Extract ## Context and ## Conventions sections
    plan_context = ""
    plan_conventions = ""
    context_pattern = re.compile(r"^##\s+Context\s*$", re.IGNORECASE)
    conventions_pattern = re.compile(r"^##\s+Conventions\s*$", re.IGNORECASE)

    for i, line in enumerate(lines):
        if context_pattern.match(line):
            section_lines = []
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("## "):
                    break
                section_lines.append(lines[j])
            plan_context = "\n".join(section_lines).strip()
        elif conventions_pattern.match(line):
            section_lines = []
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("## "):
                    break
                section_lines.append(lines[j])
            plan_conventions = "\n".join(section_lines).strip()

    # Split into task sections
    tasks: list[Task] = []
    task_pattern = re.compile(r"^##\s+Task:\s*(.+)$", re.IGNORECASE)
    files_pattern = re.compile(r"^(?:Files|Scope):\s*(.+)$", re.IGNORECASE)
    depends_pattern = re.compile(r"^(?:Depends|Dependencies|After):\s*(.+)$", re.IGNORECASE)

    current_title: str | None = None
    current_lines: list[str] = []
    current_files: list[str] = []
    current_depends: list[str] = []

    def _flush():
        if current_title:
            task_id = f"task-{len(tasks) + 1}"
            desc = "\n".join(current_lines).strip()
            tasks.append(Task(
                id=task_id,
                title=current_title,
                description=desc,
                files=list(current_files),
                depends_on=list(current_depends),
            ))

    for line in lines:
        m = task_pattern.match(line)
        if m:
            _flush()
            current_title = m.group(1).strip()
            current_lines = []
            current_files = []
            current_depends = []
            continue

        if current_title is None:
            continue

        fm = files_pattern.match(line.strip())
        if fm:
            current_files = [f.strip() for f in fm.group(1).split(",") if f.strip()]
            continue

        dm = depends_pattern.match(line.strip())
        if dm:
            current_depends = [d.strip() for d in dm.group(1).split(",") if d.strip()]
            continue

        current_lines.append(line)

    _flush()

    # Resolve dependency slugs to task IDs
    slug_to_id = {t.slug: t.id for t in tasks}
    for task in tasks:
        task.depends_on = [slug_to_id.get(d, d) for d in task.depends_on]

    return Plan(
        title=plan_title,
        tasks=tasks,
        source=path,
        context=plan_context,
        conventions=plan_conventions,
    )
