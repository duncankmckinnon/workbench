# Workbench

[![CI](https://github.com/duncankmckinnon/workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/duncankmckinnon/workbench/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/wbcli?v=2)](https://pypi.org/project/wbcli/)
[![Python](https://img.shields.io/pypi/pyversions/wbcli?v=2)](https://pypi.org/project/wbcli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Multi-agent orchestrator that dispatches AI coding agents in parallel across isolated git worktrees.

Write a markdown plan, run `wb run plan.md`, and workbench parses it into tasks, groups them into dependency waves, and runs each task through an **implement → test → review → fix** pipeline.

## Requirements

**Required:**

- **Python 3.11+**
  - macOS: `brew install python` or [python.org](https://www.python.org/downloads/)
  - Linux: `apt install python3` / `dnf install python3`
  - Windows: [python.org](https://www.python.org/downloads/) or `winget install Python.Python.3.13`

- **Git**
  - macOS: `xcode-select --install` or `brew install git`
  - Linux: `apt install git` / `dnf install git`
  - Windows: [git-scm.com](https://git-scm.com/downloads) or `winget install Git.Git`

- **An agent CLI** — at least one of:
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (default)
  - [Gemini CLI](https://github.com/google-gemini/gemini-cli)
  - [Codex](https://github.com/openai/codex)
  - Any custom CLI via `.workbench/agents.yaml`

**Optional:**

- **tmux** — enables live monitoring of agent sessions. Use `--no-tmux` to run without it.
  - macOS: `brew install tmux`
  - Linux: `apt install tmux` / `dnf install tmux`
  - Windows: available via WSL

## Install

```bash
pip install wbcli
```

## Getting started

### 1. Set up your repo

```bash
wb setup
```

This creates a `.workbench/` directory in your repo and installs the bundled skill file for your agent platform. The skill teaches your agent how to write effective workbench plans and use the CLI to configure and execute them.

```bash
wb setup                           # auto-detect agent, install skills locally
wb setup --agent claude            # install to <repo>/.claude/skills/ + .agents/skills/
wb setup --agent gemini            # install to <repo>/.agents/skills/
wb setup --agent manual            # print paths for manual setup
wb setup --global                  # install skills to user-level paths only (no .workbench/)
wb setup --global --agent claude   # install to ~/.claude/skills/
wb setup --global --agent gemini   # install to ~/.agents/skills/
wb setup --symlink                 # symlink instead of copy (stays in sync with updates)
wb setup --update                  # force-update skills to the latest installed version
wb setup --profile                 # also create a profile.yaml with the detected agent
```

If the skill file already exists and is unchanged, it's skipped. If it differs, you'll be prompted before overwriting. Use `--update` to force-overwrite.

### 2. Write a plan

Create a markdown file (e.g. `plan.md`) with tasks for workbench to execute:

```markdown
# Plan title

## Context

<Background about the project, what's being built, and why.
Injected into every agent's prompt so each task has full context.>

## Conventions

<Project-specific patterns agents must follow: language version,
test framework, import style, naming conventions, etc.
Also injected into every agent's prompt.>

## Task: Short title
Files: path/to/file.py, path/to/other.py

<Detailed description of what to implement. Each task runs in an
isolated git worktree — the agent only sees this description,
not the rest of the plan. Be specific and self-contained.>

## Task: Another task
Files: path/to/different.py
Depends: short-title

<This task depends on "Short title" (referenced by its slug).
It won't start until the dependency completes. Describe the
interfaces from the earlier task that this task needs.>
```

**Plan sections:**

| Section | Purpose |
|---|---|
| `# Title` | Plan name (shown in status output) |
| `## Context` | Project background — injected into every agent's prompt |
| `## Conventions` | Code style rules — injected into every agent's prompt |
| `## Task: <title>` | A unit of work, becomes an independent agent session |
| `Files:` | File ownership — prevents parallel tasks from conflicting |
| `Depends:` | Task slugs this depends on (title → lowercase, non-alphanumeric → `-`) |

Tasks without dependencies run in the earliest wave. Keep titles short (2-4 words) — they become dependency slugs.

Use `wb preview plan.md` to dry-run and verify tasks and waves before executing.

### 3. Run the plan

```bash
wb run plan.md
```

Workbench parses the plan, groups tasks into dependency waves, creates isolated git worktrees, and dispatches agents in parallel. Each task goes through:

```
implement → test → fix  → review → fix (retry up to --max-retries)
```

After each wave, successful task branches are merged into a session branch (`workbench-N`). Merge conflicts between parallel branches are automatically resolved by a merger agent. Task outcomes are tracked in `.workbench/status.json` as each task completes.

### 4. Handle failures

If some tasks fail, you have options:

```bash
# Re-run the same plan, skipping tasks that already succeeded
wb run plan.md -b workbench-1 --only-failed

# Auto-retry tasks that crashed (not those that exhausted fix retries)
wb run plan.md --retry-failed

# Stop immediately if any task in a wave fails
wb run plan.md --fail-fast

# Combine: retry crashes, then stop if still failing
wb run plan.md --retry-failed --fail-fast
```

`--retry-failed` distinguishes between transient failures (agent crash, timeout) and deliberate failures (exhausted all fix cycles). Only transient failures are retried.

`--only-failed` reads `.workbench/status.json` to determine which tasks already completed. It requires `-b` to specify the session branch to resume.

### 5. Merge unmerged branches

If a run was interrupted or some merges failed due to conflicts, use `wb merge` to attempt merging without re-running pipelines:

```bash
wb merge -b workbench-1
```

This reads `.workbench/status.json`, finds tasks with `status=done` that haven't been merged yet, and attempts each merge. Conflicts are handled by a merge resolver agent. Branches that were already merged manually (via git) are detected and skipped.

### 6. Monitor progress

A live status table shows task progress in the terminal. With tmux (default), you can also attach to watch any agent work:

```bash
tmux attach -t wb-task-1-implementor
```

Sessions are named `wb-task-<N>-<role>`.

## Branching strategy

When you run `wb run plan.md`, workbench creates this branch structure:

```
main (or --base branch)
 └── workbench-N (or --name)         ← session branch (all work merges here)
      ├── wb/task-1-short-title       ← worktree branch for task 1
      ├── wb/task-2-another-task   ← worktree branch for task 2
```

Each task gets its own branch and worktree. Tasks in the same wave run in parallel. After a wave completes, successful task branches are merged into the session branch. If merge conflicts arise between parallel branches, a merger agent resolves them automatically. The next wave then branches from the updated session branch.

When all waves finish, the session branch (`workbench-N`) contains the combined work and is ready for review or merging into your base branch.

By default, workbench fetches `origin/main` and creates the session branch from the latest remote state.

| Flag | Session branch | Base | Source | Use case |
|------|----------------|------|--------|----------|
| *(default)* | `workbench-N` | `main` | `origin/main` (fetched) | Start from latest remote |
| `--name my-feature` | `my-feature` | `main` | `origin/main` (fetched) | Named session branch |
| `--local` | `workbench-N` | `main` | local `main` | Build on unpushed local work |
| `--base <branch>` | `workbench-N` | `<branch>` | `origin/<branch>` (fetched) | Branch from a specific remote branch |
| `--base <branch> --local` | `workbench-N` | `<branch>` | local `<branch>` | Branch from a local feature branch |
| `-b my-session` | `my-session` | *(existing)* | *(existing)* | Resume a previous session |

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
| `wb merge -b <branch>` | Merge completed-but-unmerged task branches |
| `wb preview <plan>` | Dry-run: show parsed tasks and waves |
| `wb setup` | Create `.workbench/`, install skills, and optionally create a profile |
| `wb status` | Show active worktrees |
| `wb stop` | Kill all running agent tmux sessions |
| `wb clean` | Remove all workbench worktrees and `wb/` branches |
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
| `--retry-failed` | Auto-retry tasks that crashed (not those that exhausted fix retries) |
| `--fail-fast` | Stop after the first wave with any failed tasks |
| `--only-failed` | Skip completed tasks from a prior run (requires `-b`) |
| `--cleanup` | Remove worktrees after completion |
| `--keep-branches` | Keep task branches after merging (default: auto-delete on success) |
| `--repo PATH` | Repository path (auto-detected if omitted) |
| `--profile PATH` | Use a specific profile.yaml |
| `--profile-name NAME` | Use a named profile (`profile.<name>.yaml`) |
| `--*-directive TEXT` | Override instructions for a specific agent role |

### `wb setup`

| Flag | Description |
|---|---|
| `--agent NAME` | Target platform: `claude`, `gemini`, `cursor`, `codex`, `manual` (auto-detected if omitted) |
| `--global` | Install skills to user-level paths only (skip `.workbench/` creation) |
| `--symlink` | Symlink instead of copy (stays in sync with package updates) |
| `--profile` | Also create a profile.yaml with the detected agent |
| `--update` | Force-update skills to the latest version |
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb merge`

| Flag | Description |
|---|---|
| `-b NAME` / `--session-branch` | Session branch to merge into (required) |
| `--agent CMD` | Agent CLI for conflict resolution (default: `claude`) |
| `--no-tmux` | Run resolver agents as subprocesses instead of tmux |
| `--keep-branches` | Keep task branches after merging |
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
