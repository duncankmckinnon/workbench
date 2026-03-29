# Workbench

[![CI](https://github.com/duncankmckinnon/workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/duncankmckinnon/workbench/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/wbcli)](https://pypi.org/project/wbcli/)
[![Python](https://img.shields.io/pypi/pyversions/wbcli)](https://pypi.org/project/wbcli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Multi-agent orchestrator that dispatches AI coding agents in parallel across isolated git worktrees.

Write a markdown plan, run `wb run plan.md`, and workbench parses it into tasks, groups them into dependency waves, and runs each task through an implement → test → review → fix pipeline.

## Requirements

- Python 3.11+
- tmux (recommended — `brew install tmux` / `apt install tmux`). Use `--no-tmux` without it.
- An agent CLI: [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (default), [Codex](https://github.com/openai/codex), or any custom CLI.

## Install

```bash
pip install wbcli
```

For development:

```bash
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

## TDD mode

Run with `--tdd` to write tests before implementation:

```bash
wb run plan.md --tdd
```

Pipeline becomes: **write tests → implement → verify tests → review → fix**

The tester agent writes comprehensive failing tests based on the task description, then the implementor writes code to make them pass. After that, normal test verification and review proceed as usual.

Cannot be combined with `--skip-test`.

## Stopping agents

```bash
wb stop              # kill all active agent tmux sessions
wb stop --cleanup    # also remove worktrees and branches
```

## CLI reference

| Command | Description |
|---|---|
| `wb run <plan>` | Execute a plan |
| `wb preview <plan>` | Dry-run preview of tasks and waves |
| `wb status` | Show active worktrees |
| `wb stop` | Stop all running agents and optionally clean up |
| `wb clean` | Remove all workbench worktrees and branches |
| `wb init` | Set up workbench for your agent platform |

## Conventions
- Python 3.11+, type hints
- Tests: pytest, run with `uv run pytest`

| Flag | Description |
|---|---|
| `-j N` | Max concurrent tasks (default: 4) |
| `--tdd` | Test-driven: write tests first, then implement |
| `--skip-test` | Skip the test phase |
| `--skip-review` | Skip the review phase |
| `--max-retries N` | Max fix cycles per task (default: 2) |
| `--agent CMD` | Agent CLI command (default: `claude`) |
| `--no-tmux` | Run agents as subprocesses instead of in tmux sessions |
| `--session-branch NAME` | Resume an existing session branch |
| `--start-wave N` | Skip already-merged waves |
| `--cleanup` | Remove worktrees after completion |
| `--repo PATH` | Repository path (auto-detected if omitted) |

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

## Agent commands

Workbench bundles guidance files that teach agents how to write plans. `wb init` installs them in the right format for each platform:

```bash
wb init --agent claude     # installs as /use-workbench command in ~/.claude/commands/
wb init --agent cursor     # installs as rule in .cursor/rules/
wb init --agent codex      # appends to .codex/instructions.md
wb init --agent manual     # prints paths for manual setup
wb init --symlink          # symlink instead of copy (for development)
```

`wb setup` combines `.workbench/` creation with command installation in one step.

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

Version is derived from git tags via `setuptools-scm`. Published to PyPI as [`wbcli`](https://pypi.org/project/wbcli/). To release:

```bash
git tag v0.1.0
git push origin v0.1.0    # triggers CI → PyPI + GitHub Release
```

## License

MIT
