# Workbench

[![CI](https://github.com/duncankmckinnon/workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/duncankmckinnon/workbench/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/wbcli?v=2)](https://pypi.org/project/wbcli/)
[![Python](https://img.shields.io/pypi/pyversions/wbcli?v=2)](https://pypi.org/project/wbcli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Multi-agent orchestrator that dispatches AI coding agents in parallel across isolated git worktrees.

Write a markdown plan, run `wb run plan.md`, and workbench parses it into tasks, groups them into dependency waves, and runs each task through an **implement → test → review → fix** pipeline.

## Requirements

- Python 3.11+
- tmux (recommended — `brew install tmux` / `apt install tmux`). Use `--no-tmux` without it.
- An agent CLI: [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (default), [Gemini CLI](https://github.com/google-gemini/gemini-cli), [Codex](https://github.com/openai/codex), or any custom CLI.

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
wb init                            # install skills only (auto-detects agent platform)
wb init --agent claude             # install to ~/.claude/skills/
wb init --agent gemini             # install to ~/.agents/skills/
wb init --agent codex              # install as skill in .codex/instructions.md
wb init --agent cursor             # install as skill in .cursor/rules/
wb init --agent manual             # print paths for manual setup
wb init --agent claude --local     # install to <repo>/.claude/skills/ + .agents/skills/
wb init --agent gemini --local     # install to <repo>/.agents/skills/
wb init --symlink                  # symlink instead of copy (stays in sync with updates)
wb init --update                   # force-update skills to the latest installed version
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

## Profiles

Profiles configure which agent CLI and instructions are used for each pipeline role. When no profile exists, built-in defaults apply.

### Create a profile

```bash
wb profile init                                            # create .workbench/profile.yaml from defaults
wb profile init --global                                   # create ~/.workbench/profile.yaml
wb profile init --set reviewer.agent=gemini                # create with inline overrides
wb profile init --set reviewer.agent=gemini --set tester.directive_extend="Run with -x"
```

### Named profiles

Create multiple profiles for different workflows:

```bash
wb profile init --name fast --set reviewer.agent=gemini --set implementor.agent=codex
wb profile init --name security --set reviewer.directive="Focus only on security vulnerabilities."
wb run plan.md --profile-name fast                         # use a named profile
```

Named profiles are stored as `profile.<name>.yaml` alongside the default `profile.yaml`.

### Customize roles

```bash
wb profile set reviewer.agent gemini                       # update default profile
wb profile set tester.directive_extend "Run pytest with -x flag."
wb profile set reviewer.agent codex --name fast            # update a named profile
```

Or edit `.workbench/profile.yaml` directly:

```yaml
roles:
  reviewer:
    agent: gemini
    directive: "Focus on security and correctness."
  tester:
    directive_extend: "Also check edge cases for null inputs."
```

Use `directive` to replace the default instructions, or `directive_extend` to append to them.

### Profile fields

| Role | Description |
|---|---|
| `implementor` | Writes code to fulfill the task |
| `tester` | Runs and writes tests, reports PASS/FAIL |
| `reviewer` | Reviews the diff for correctness and quality |
| `fixer` | Addresses feedback from failed tests or reviews |
| `merger` | Resolves merge conflicts between parallel branches |

Each role supports these fields:

| Field | Description |
|---|---|
| `agent` | CLI command to use for this role (default: `claude`) |
| `directive` | Full replacement for the role's default instructions |
| `directive_extend` | Text appended to the default instructions (cannot be combined with `directive`) |

### View and compare

```bash
wb profile show                    # print resolved profile
wb profile show --name fast        # show a named profile
wb profile diff                    # show differences from defaults
wb profile diff --name fast        # diff a named profile
```

### Merge order

Profiles merge in order: built-in defaults < `~/.workbench/profile.yaml` < `.workbench/profile.yaml` < `--profile` flag < CLI flags. Named profiles (`--profile-name`) replace the default filename at each level.

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
| `wb clean` | Remove all workbench worktrees and `wb/` branches |
| `wb init` | Install skills for an agent platform |
| `wb setup` | Create `.workbench/` and install skills |
| `wb profile init` | Create profile.yaml from defaults |
| `wb profile show` | Show resolved profile |
| `wb profile set <key> <value>` | Update a profile field |
| `wb profile diff` | Show differences from defaults |

### `wb run`

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
| `--keep-branches` | Keep task branches after merging (default: auto-delete on success) |
| `--repo PATH` | Repository path (auto-detected if omitted) |
| `--profile PATH` | Use a specific profile.yaml |
| `--profile-name NAME` | Use a named profile (`profile.<name>.yaml`) |
| `--*-directive TEXT` | Override instructions for a specific agent role |

### `wb init`

| Flag | Description |
|---|---|
| `--agent NAME` | Target platform: `claude`, `gemini`, `cursor`, `codex`, `manual` (auto-detected if omitted) |
| `--local` | Install skills to repo-local paths instead of global |
| `--symlink` | Symlink instead of copy (stays in sync with package updates) |
| `--profile` | Also create a profile.yaml with the detected agent |
| `--update` | Force-update skills to the latest version |

### `wb setup`

| Flag | Description |
|---|---|
| `--agent NAME` | Target platform (auto-detected if omitted) |
| `--symlink` | Symlink skills instead of copy |
| `--profile` | Also create a profile.yaml with the detected agent |
| `--update` | Force-update skills to the latest version |
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb stop`

| Flag | Description |
|---|---|
| `--cleanup` | Also remove worktrees and `wb/` branches |
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb status`

| Flag | Description |
|---|---|
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb clean`

| Flag | Description |
|---|---|
| `--repo PATH` | Repository path (auto-detected if omitted) |
| `--yes` | Skip confirmation prompt |

### `wb profile init`

| Flag | Description |
|---|---|
| `--global` | Create in `~/.workbench/` instead of `.workbench/` |
| `--name NAME` | Create a named profile (`profile.<name>.yaml`) |
| `--set KEY=VALUE` | Set role fields inline (repeatable) |
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb profile show`

| Flag | Description |
|---|---|
| `--name NAME` | Show a named profile |
| `--profile PATH` | Path to a specific profile.yaml |
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb profile set`

| Flag | Description |
|---|---|
| `--global` | Update `~/.workbench/` instead of local |
| `--name NAME` | Update a named profile |
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb profile diff`

| Flag | Description |
|---|---|
| `--name NAME` | Diff a named profile |
| `--profile PATH` | Path to a specific profile.yaml |
| `--repo PATH` | Repository path (auto-detected if omitted) |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, code style, testing, and release instructions.

## License

MIT