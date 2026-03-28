# Workbench

Multi-agent orchestrator that dispatches AI coding agents in parallel across isolated git worktrees.

Write a markdown plan, run `wb run plan.md`, and workbench parses it into tasks, groups them into dependency waves, and runs each task through an implement → test → review → fix pipeline.

## Requirements

- Python 3.11+
- tmux (recommended — `brew install tmux` / `apt install tmux`). Use `--no-tmux` without it.
- An agent CLI: [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (default), [Codex](https://github.com/openai/codex), or any custom CLI.

## Install

```bash
pip install -e .
pip install -e ".[dev]"   # includes pytest
```

## Usage

```bash
wb setup                          # create .workbench/ and install skills
wb preview plan.md                # dry-run: show tasks and waves
wb run plan.md                    # run the plan
wb run plan.md -j 6 --no-tmux    # 6 parallel agents, no tmux
wb run plan.md --agent codex      # use codex instead of claude
wb status                         # show active worktrees
wb clean                          # remove all worktrees and wb/ branches
```

### Resuming a run

```bash
wb run plan.md -b workbench-2 -w 3   # resume session branch from wave 3
```

### Directive overrides

Override the instructions given to any agent role:

```bash
wb run plan.md --reviewer-directive "Focus only on security issues."
wb run plan.md --tester-directive "Run pytest with -x flag, fail fast."
```

Available: `--implementor-directive`, `--tester-directive`, `--reviewer-directive`, `--fixer-directive`.

## Plan format

```markdown
# Plan Title

## Context
What this project is and what we're building.

## Conventions
- Python 3.11+, type hints
- Tests: pytest, run with `uv run pytest`

## Task: Auth middleware
Files: src/auth.py, src/middleware.py
Depends: database-setup

Implement JWT authentication middleware...

## Task: Database setup
Files: src/db.py, migrations/

Set up SQLAlchemy models...
```

- `## Context` and `## Conventions` are injected into every agent's prompt.
- `Files:` declares which files the task owns (prevents parallel conflicts).
- `Depends:` references other tasks by slug (title lowercased, non-alphanumeric → `-`).
- Tasks without dependencies run in the earliest wave. Keep titles short (2-4 words) — they become dependency slugs.

## Agent pipeline

Each task runs through:

```
implement → test → review → fix (retry up to --max-retries)
```

Skip stages with `--skip-test` or `--skip-review`. Merge conflicts between parallel branches are automatically resolved by a merger agent.

## Custom agents

Define adapters in `.workbench/agents.yaml`:

```yaml
agents:
  my-agent:
    command: my-agent-cli
    args: ["--headless", "{prompt}"]
    output_format: json
    json_result_key: result
    json_cost_key: cost_usd
```

```bash
wb run plan.md --agent my-agent
```

## Skills

Workbench bundles skill files that teach agents how to write plans. Install them for your platform:

```bash
wb init --agent claude     # copies to ~/.claude/commands/
wb init --agent cursor     # copies to .cursor/rules/
wb init --agent codex      # appends to .codex/instructions.md
wb init --agent manual     # prints paths for manual setup
wb init --symlink          # symlink instead of copy (for development)
```

`wb setup` combines `.workbench/` creation with skill installation in one step.

## Monitoring

With tmux (default), attach to watch an agent work:

```bash
tmux attach -t wb-task-1-implementor
```

Sessions are named `wb-task-<N>-<role>`.

## CLI reference

| Command | Description |
|---|---|
| `wb run <plan>` | Execute a plan |
| `wb preview <plan>` | Show tasks and waves |
| `wb status` | Show active worktrees |
| `wb clean` | Remove worktrees and branches |
| `wb init` | Install skills for an agent platform |
| `wb setup` | Create `.workbench/` and install skills |

| `wb run` flag | Description |
|---|---|
| `-j N` | Max concurrent agents (default: 4) |
| `-r N` | Max fix retries per stage (default: 2) |
| `--skip-test` | Skip testing phase |
| `--skip-review` | Skip review phase |
| `--agent CMD` | Agent CLI (default: `claude`) |
| `--no-tmux` | Raw subprocess instead of tmux |
| `-b NAME` | Resume a session branch |
| `-w N` | Start from wave N |
| `--cleanup` | Remove worktrees after completion |
| `--*-directive` | Override role instructions |

## Development

```bash
pip install -e ".[dev]"
pytest                    # 114 tests
```

Version is derived from git tags via `setuptools-scm`. To release:

```bash
git tag v0.1.0
git push origin v0.1.0    # triggers CI → PyPI + GitHub Release
```

## License

MIT
