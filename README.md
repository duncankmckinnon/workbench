# Workbench

[![CI](https://github.com/duncankmckinnon/workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/duncankmckinnon/workbench/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/wbcli)](https://pypi.org/project/wbcli/)
[![Python](https://img.shields.io/pypi/pyversions/wbcli)](https://pypi.org/project/wbcli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Multi-agent orchestrator that dispatches AI coding agents in parallel across isolated git worktrees.

Write a markdown plan, run `wb run plan.md`, and workbench parses it into tasks, groups them into dependency waves, and runs each task through an **implement → test → review → fix** pipeline.

## Requirements

- Python 3.11+
- tmux (recommended — `brew install tmux` / `apt install tmux`). Use `--no-tmux` without it.
- An agent CLI: [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (default), [Codex](https://github.com/openai/codex), or any custom CLI.

## Install

```bash
pip install wbcli
```

## Getting started

### 1. Set up your repo

```bash
wb setup
```

This creates a `.workbench/` directory in your repo and installs the bundled skill file for your agent platform. The skill teaches your agent how to write effective workbench plans and use the cli to configure and execute them.

You can also run setup in two steps:

```bash
wb init                    # install skills only (auto-detects agent platform)
wb init --agent claude     # install as skill in ~/.claude/skills/
wb init --agent cursor     # install as skill in .cursor/rules/
wb init --agent codex      # install as skill in .codex/instructions.md
wb init --agent manual     # print paths for manual setup
wb init --symlink          # symlink instead of copy (stays in sync with updates)
```

If the skill file already exists and is unchanged, it's skipped. If it differs, you'll be prompted before overwriting.

### 2. Write a plan

Create a markdown file with tasks for workbench to execute:

```markdown
# Add user authentication

## Context

FastAPI app with SQLAlchemy ORM. Auth should use JWT tokens.

## Conventions

- Python 3.12, type hints on all signatures
- Tests: pytest, run with `pytest tests/ -v`
- Imports: stdlib → third-party → local

## Task: JWT tokens
Files: src/auth/tokens.py, tests/test_tokens.py

Create `create_token(user_id: str) -> str` and `verify_token(token: str) -> dict`.
Tokens expire after 24h. Secret from `AUTH_SECRET` env var.

## Task: Auth middleware
Files: src/auth/middleware.py, tests/test_middleware.py
Depends: jwt-tokens

FastAPI dependency `require_auth(request: Request) -> User` that extracts
Bearer token from Authorization header and verifies it.
```

Key plan elements:
- `## Context` and `## Conventions` are injected into every agent's prompt
- `## Task: <title>` defines each unit of work
- `Files:` declares file ownership (prevents parallel conflicts)
- `Depends:` references other tasks by slug (title → lowercase, non-alphanumeric → `-`)
- Tasks without dependencies run in the earliest wave

Use `wb preview plan.md` to dry-run and verify tasks and waves before executing.

### 3. Run the plan

```bash
wb run plan.md
```

Workbench parses the plan, groups tasks into dependency waves, creates isolated git worktrees, and dispatches agents in parallel. Each task goes through:

```
implement → test → review → fix (retry up to --max-retries)
```

After each wave, successful task branches are merged into a session branch (`workbench-N`). Merge conflicts between parallel branches are automatically resolved by a merger agent.

### 4. Monitor progress

A live status table shows task progress in the terminal. With tmux (default), you can also attach to watch any agent work:

```bash
tmux attach -t wb-task-1-implementor
```

Sessions are named `wb-task-<N>-<role>`.

## Branching strategy

When you run `wb run plan.md`, workbench creates this branch structure:

```
main (or --base branch)
 └── workbench-N                ← session branch (all work merges here)
      ├── wb/task-1-jwt-tokens       ← worktree branch for task 1
      ├── wb/task-2-auth-middleware   ← worktree branch for task 2
      └── wb/task-3-api-endpoints    ← worktree branch for task 3
```

Each task gets its own branch and worktree. Tasks in the same wave run in parallel. After a wave completes, successful task branches are merged into the session branch. If merge conflicts arise between parallel branches, a merger agent resolves them automatically. The next wave then branches from the updated session branch.

When all waves finish, the session branch (`workbench-N`) contains the combined work and is ready for review or merging into your base branch.

By default, workbench fetches `origin/main` and creates the session branch from the latest remote state.

| Flag | Base | Source | Use case |
|------|------|--------|----------|
| *(default)* | `main` | `origin/main` (fetched) | Start from latest remote |
| `--local` | `main` | local `main` | Build on unpushed local work |
| `--base <branch>` | `<branch>` | `origin/<branch>` (fetched) | Branch from a specific remote branch |
| `--base <branch> --local` | `<branch>` | local `<branch>` | Branch from a local feature branch |
| `-b workbench-3` | *(existing)* | *(existing)* | Resume a previous session |

## TDD mode

```bash
wb run plan.md --tdd
```

Pipeline becomes: **write tests → implement → verify tests → review → fix**

The tester writes comprehensive failing tests first. The implementor writes code to make them pass and reports whether the tests are comprehensive. Cannot be combined with `--skip-test`.

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

## Directive overrides

Override the instructions given to any agent role:

```bash
wb run plan.md --reviewer-directive "Focus only on security issues."
wb run plan.md --tester-directive "Run pytest with -x flag, fail fast."
```

Available: `--implementor-directive`, `--tester-directive`, `--reviewer-directive`, `--fixer-directive`.

## CLI reference

### Commands

| Command | Description |
|---|---|
| `wb run <plan>` | Execute a plan with parallel agents |
| `wb preview <plan>` | Dry-run: show parsed tasks and waves |
| `wb status` | Show active worktrees |
| `wb stop` | Kill all running agent tmux sessions |
| `wb stop --cleanup` | Also remove worktrees and branches |
| `wb clean` | Remove all workbench worktrees and `wb/` branches |
| `wb init` | Install skills for an agent platform |
| `wb setup` | Create `.workbench/` and install skills |

### `wb run` flags

| Flag | Description |
|---|---|
| `-j N` | Max concurrent agents (default: 4) |
| `--max-retries N` / `-r N` | Max fix cycles per failed stage (default: 2) |
| `--skip-test` | Skip the test phase |
| `--skip-review` | Skip the review phase |
| `--tdd` | Test-driven: write tests first, then implement |
| `--agent CMD` | Agent CLI command (default: `claude`) |
| `--no-tmux` | Run agents as subprocesses instead of tmux |
| `--base BRANCH` | Base branch to start from (default: `main`) |
| `--local` | Branch from local ref instead of fetching origin |
| `-b NAME` / `--session-branch` | Resume an existing session branch |
| `-w N` / `--start-wave` | Skip already-completed waves |
| `--cleanup` | Remove worktrees after completion |
| `--repo PATH` | Repository path (auto-detected if omitted) |
| `--*-directive TEXT` | Override instructions for a specific agent role |

### `wb init` flags

| Flag | Description |
|---|---|
| `--agent NAME` | Target platform: `claude`, `cursor`, `codex`, `manual` (auto-detected if omitted) |
| `--symlink` | Symlink instead of copy (stays in sync with package updates) |

### `wb stop` flags

| Flag | Description |
|---|---|
| `--cleanup` | Also remove worktrees and `wb/` branches |
| `--repo PATH` | Repository path (auto-detected if omitted) |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, code style, testing, and release instructions.

## License

MIT
