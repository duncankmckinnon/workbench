# Workbench

Lightweight multi-agent orchestrator that runs AI coding agents in parallel to implement structured development plans.

## Requirements

- **Python 3.11+**
- **tmux** (recommended) — agents run in visible tmux sessions so you can watch them work. Pass `--no-tmux` to run without it.
- **An agent CLI** on your PATH:
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code): `npm install -g @anthropic-ai/claude-code`
  - [Codex](https://github.com/openai/codex): `npm install -g @openai/codex`

## Installation

```bash
pip install -e .

# Or with dev dependencies for running tests:
pip install -e ".[dev]"
```

## Quick start

```bash
# Preview a plan (dry-run showing tasks and waves)
wb preview plan.md

# Run a plan
wb run plan.md

# Run without tmux
wb run plan.md --no-tmux

# Use codex instead of claude
wb run plan.md --agent codex
```

## How it works

1. Write a markdown plan with `## Task: <title>` sections, listing relevant files and dependencies.
2. Run `wb run <plan.md>` — Workbench parses the plan, groups tasks into waves by dependency order, and creates an isolated git worktree per task.
3. Each task runs through a pipeline of specialized agents (implementor, tester, reviewer, fixer) with automatic retry on failure.
4. Completed branches are merged into a session branch for review.

## CLI reference

| Command | Description |
|---|---|
| `wb run <plan>` | Execute a plan |
| `wb preview <plan>` | Dry-run preview of tasks and waves |
| `wb status` | Show active worktrees |
| `wb clean` | Remove all workbench worktrees and branches |
| `wb init` | Set up workbench for your agent platform |

### `wb run` flags

| Flag | Description |
|---|---|
| `-j N` | Max concurrent tasks (default: 4) |
| `--skip-test` | Skip the test phase |
| `--skip-review` | Skip the review phase |
| `--max-retries N` | Max fix cycles per task (default: 2) |
| `--agent CMD` | Agent CLI command (default: `claude`) |
| `--no-tmux` | Run agents as subprocesses instead of in tmux sessions |
| `--session-branch NAME` | Resume an existing session branch |
| `--start-wave N` | Skip already-merged waves |
| `--cleanup` | Remove worktrees after completion |
| `--repo PATH` | Repository path (auto-detected if omitted) |

## Supported agents

| Agent | CLI command | Notes |
|---|---|---|
| Claude Code | `claude` | Default. Supports `--print` mode for headless execution. |
| Codex | `codex` | Uses `--full-auto` mode. |

Select an agent with `--agent`:

```bash
wb run plan.md --agent codex
```

## Custom agents

Define custom agent adapters in `.workbench/agents.yaml`:

```yaml
agents:
  my-agent:
    command: my-agent-cli
    args:
      - "--headless"
      - "{prompt}"
    output_format: json        # "text" (default) or "json"
    json_result_key: result    # key containing the result in JSON output
    json_cost_key: cost_usd    # key containing cost in JSON output
```

The `{prompt}` placeholder in `args` is replaced with the agent prompt at runtime. Then use it with:

```bash
wb run plan.md --agent my-agent
```

## Monitoring agents

When running with tmux (the default), each agent gets a named tmux session. Attach to watch an agent work:

```bash
# List active sessions
tmux ls

# Attach to a specific task's agent
tmux attach -t wb-task-1-implementor
```

Session names follow the pattern `wb-task-<N>-<role>` where role is `implementor`, `tester`, `reviewer`, or `fixer`.

## Setup with `wb init`

Run `wb init` to configure workbench for your agent platform:

```bash
wb init          # interactive — prompts for platform
wb init claude   # set up for Claude Code
wb init cursor   # set up for Cursor
wb init codex    # set up for Codex
wb init manual   # minimal setup, no agent-specific config
```

The init command creates a `.workbench/` directory in your project and installs skill files that teach your agent how to work within the workbench pipeline (e.g., how to implement tasks, run tests, and handle reviews).

## Skills

Workbench bundles reusable skill files in `workbench/skills/` that provide agent instructions for each pipeline role. Running `wb init` copies these into your project's `.workbench/` directory for your chosen agent platform.

During development on workbench itself, use `--symlink` to symlink skill files instead of copying, so changes to the source skills are reflected immediately:

```bash
wb init claude --symlink
```

## Merge conflict resolution

When multiple tasks run in parallel, their branches may conflict when merging into the session branch. Workbench automatically attempts to resolve merge conflicts by running a resolver agent. If automatic resolution fails, the task is marked as failed and you can resolve the conflict manually.

## Plan format

Plans are markdown files with `## Task:` sections:

```markdown
## Task: Add user authentication
Files: src/auth.py, src/middleware.py
Depends: database-setup

Implement JWT-based authentication middleware...

## Task: Database setup
Files: src/db.py, migrations/

Set up SQLAlchemy models and migration scripts...
```

- **Files**: Lists files the task will read or modify (used for context).
- **Depends**: References other task titles to establish execution order. Tasks without dependencies run in the earliest wave.

## Running tests

```bash
pip install -e ".[dev]" && pytest
```

## License

[MIT](LICENSE)
