# Workbench

Lightweight multi-agent orchestrator for iterative software development. Workbench automates a structured **implement → test → review → fix** pipeline using AI coding agents (defaults to [Claude Code](https://docs.anthropic.com/en/docs/claude-code)).

## How it works

1. Write a markdown plan with `## Task: <title>` sections, listing relevant files and dependencies.
2. Run `wb run <plan.md>` — Workbench parses the plan, groups tasks into waves by dependency order, and creates an isolated git worktree per task.
3. Each task runs through a pipeline of specialized agents (implementor, tester, reviewer, fixer) with automatic retry on failure.
4. Completed branches are merged into a session branch for review.

## Install

```bash
pip install -e .
```

Requires Python >= 3.11 and an agent CLI (e.g., `claude`) available on your PATH.

## Usage

```bash
# Run a plan
wb run plan.md

# Preview tasks and waves without running
wb preview plan.md

# Check active worktrees
wb status

# Clean up worktrees and branches
wb clean
```

### Options

| Flag | Description |
|---|---|
| `-j N` | Max concurrent tasks (default: 4) |
| `--skip-test` | Skip the test phase |
| `--skip-review` | Skip the review phase |
| `--max-retries N` | Max fix cycles per task (default: 2) |
| `--agent CMD` | Agent CLI command (default: `claude`) |
| `--session-branch NAME` | Resume an existing session branch |
| `--start-wave N` | Skip already-merged waves |

## Plan format

```markdown
## Task: Add user authentication
Files: src/auth.py, src/middleware.py
Depends: database-setup

Implement JWT-based authentication middleware...

## Task: Database setup
Files: src/db.py, migrations/

Set up SQLAlchemy models and migration scripts...
```

## License

[MIT](LICENSE)
