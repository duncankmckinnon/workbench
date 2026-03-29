# Use Workbench

Workbench is a multi-agent orchestrator. When you see a `plan.md` file with `## Task:` sections, you can run:

```
wb run plan.md
```

This will dispatch parallel agents to implement, test, and review each task.

With `--tdd`, the pipeline becomes: **test (write failing) → implement (make pass) → test (verify) → review → fix**

In TDD mode, the tester writes comprehensive failing tests first. The implementor then writes code to make all tests pass. Normal test verification and review follow.

## Key commands

- `wb run <plan>` — execute a plan with parallel agents
- `wb preview <plan>` — dry-run to see parsed tasks and waves
- `wb status` — show active worktrees
- `wb run plan.md --tdd` — test-driven: tests first, then implement
- `wb stop` — kill all active agent sessions
- `wb stop --cleanup` — also remove worktrees and branches
- `wb clean` — remove all workbench worktrees
- `wb setup` — prepare a repo for workbench use
- `wb init` — install workbench skills for your agent platform
