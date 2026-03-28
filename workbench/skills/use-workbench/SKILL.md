# Use Workbench

Workbench is a multi-agent orchestrator. When you see a `plan.md` file with `## Task:` sections, you can run:

```
wb run plan.md
```

This will dispatch parallel agents to implement, test, and review each task.

## Key commands

- `wb run <plan>` — execute a plan with parallel agents
- `wb preview <plan>` — dry-run to see parsed tasks and waves
- `wb status` — show active worktrees
- `wb clean` — remove all workbench worktrees
- `wb setup` — prepare a repo for workbench use
- `wb init` — install workbench skills for your agent platform
