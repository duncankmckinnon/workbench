# Workbench

[![CI](https://github.com/duncankmckinnon/workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/duncankmckinnon/workbench/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/duncankmckinnon/workbench/graph/badge.svg)](https://codecov.io/gh/duncankmckinnon/workbench)
[![PyPI](https://img.shields.io/pypi/v/wbcli?v=2)](https://pypi.org/project/wbcli/)
[![Homebrew](https://img.shields.io/badge/homebrew-duncankmckinnon%2Ftap-orange?logo=homebrew)](https://github.com/duncankmckinnon/homebrew-tap)
[![Python](https://img.shields.io/pypi/pyversions/wbcli?v=3)](https://pypi.org/project/wbcli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Multi-agent orchestrator that dispatches AI coding agents in parallel across isolated git worktrees.

Write a markdown plan, run `wb run plan.md`, and workbench parses it into tasks, groups them into dependency waves, and runs each task through an **implement → test → review → fix** pipeline.

## Install

**Homebrew (macOS / Linux):**

```bash
brew install duncankmckinnon/tap/workbench
```

Pulls in Python 3.12, git, and tmux automatically.

**Python package (any platform):**

```bash
pip install wbcli
# or
uv tool install wbcli
# or
pipx install wbcli
```

Requires Python 3.11+ and git on `$PATH`. Install tmux separately for live agent monitoring (use `--no-tmux` to skip it).

Workbench dispatches to any agent CLI you wire up. Adapters live in `.workbench/agents.yaml`, so you can swap providers per role (implementor, tester, reviewer, planner, ...) or point at a custom CLI — workbench just shells out and parses the output. Out of the box it knows:

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (default)
- [Gemini CLI](https://github.com/google-gemini/gemini-cli)
- [Codex](https://github.com/openai/codex)
- [Cursor CLI](https://cursor.com/docs/cli/overview)
- [Copilot CLI](https://github.com/features/copilot/cli)

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

You can generate a plan automatically or write one by hand.

**Generate with `wb plan`:**

```bash
wb plan "Add JWT authentication to the FastAPI app"
wb plan "Refactor the database layer" --name db-refactor
wb plan --from existing-spec.md
wb plan "Focus on security" --from claude-plan.md --name secure-auth
```

The planner agent explores your codebase — reading project structure, existing patterns, test infrastructure, and conventions — then writes a detailed plan to `.workbench/<name>/plan.md`. On completion it prints commands for reviewing, previewing, and running the result.

Use `--from` to transform an existing document (e.g. a Claude plan, a spec, or rough notes) into workbench format. Add a prompt alongside `--from` for additional guidance on the transformation.

**Write by hand:**

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

### Plan frontmatter (optional)

Plans can declare run-time defaults in a YAML frontmatter block at the top of the file (before `# Title`). Without frontmatter, plans behave exactly as today.

```markdown
---
session_branch: workbench-auth
base: feature-auth
tdd: true
max_concurrent: 6
---
# Auth refactor

## Context
...
```

CLI flags always override frontmatter values. See [Frontmatter-readable flags](#frontmatter-readable-flags) in the CLI reference for the full schema.

### 3. Run the plan

```bash
wb run plan.md
```

Workbench parses the plan, groups tasks into dependency waves, creates isolated git worktrees, and dispatches agents in parallel. Each task goes through:

```
implement → test → fix  → review → fix (retry up to --max-retries)
```

After each wave, successful task branches are merged into a session branch (`workbench-N`). Merge conflicts between parallel branches are automatically resolved by a merger agent. Task outcomes are tracked in `.workbench/<plan>/status.yaml` as each task completes, keyed by session branch.

Use `--push` to push the session branch to origin when done:

```bash
wb run plan.md --push
```

### 4. Control which waves run

By default, all waves run sequentially. Use wave flags to run a subset:

```bash
wb run plan.md -w 2                          # run only wave 2
wb run plan.md --start-wave 2                # run waves 2 through end
wb run plan.md --start-wave 2 --end-wave 4   # run waves 2 through 4
```

Out-of-range values are clamped automatically: `--start-wave` defaults to 1 and `--end-wave` defaults to the last wave, with a warning printed.

### 5. Handle failures

If some tasks fail, you have options:

```bash
# Resume a session: re-run every task that isn't done + merged
wb resume workbench-1

# Same thing, manual form (use this if you need to override flags)
wb run plan.md -b workbench-1 --only-incomplete

# Auto-retry tasks that crashed (not those that exhausted fix retries)
wb run plan.md --retry-failed

# Stop immediately if any task in a wave fails
wb run plan.md --fail-fast

# Combine: retry crashes, then stop if still failing
wb run plan.md --retry-failed --fail-fast
```

`wb resume <session>` looks up the session in `.workbench/<plan>/status.yaml` (legacy `.workbench/status-*.yaml` is still read), finds the original plan, and re-runs every task that isn't `done + merged`. This includes tasks that failed AND tasks that never started (e.g., a crash before the wave reached them — the status file is seeded with every plan task as `pending` at run start so this case is handled correctly).

`--retry-failed` distinguishes between transient failures (agent crash, timeout) and deliberate failures (exhausted all fix cycles). Only transient failures are retried.

`--only-incomplete` reads the plan's status file to determine which tasks already completed. It requires `-b` to specify the session branch to resume.

You can also re-run specific tasks by ID or slug:

```bash
# Re-run a single task in an existing session
wb run plan.md -b workbench-1 --task task-2

# Re-run multiple specific tasks
wb run plan.md -b workbench-1 --task task-1 --task task-3

# Re-run a task by its slug (title converted to lowercase-dashes)
wb run plan.md -b workbench-1 --task my-feature-name

# Run specific tasks in a new session (no -b needed)
wb run plan.md --task task-2
```

`--task` accepts task IDs (e.g. `task-2`) or slugs (e.g. `my-feature-name`). Only the specified tasks run — all other tasks are left untouched. If a task has an existing branch from a prior run, it is cleaned up and started fresh. Status records for non-targeted tasks are preserved.

### 6. Merge unmerged branches

If a run was interrupted or some merges failed due to conflicts, use `wb merge` to attempt merging without re-running pipelines:

```bash
wb merge -b workbench-1
wb merge -b workbench-1 --plan plan.md    # explicit plan
wb merge -b workbench-1 --push            # merge and push to origin
```

This scans the status files for the session branch, finds tasks with `status=done` that haven't been merged yet, and attempts each merge. Conflicts are handled by a merge resolver agent. Branches that were already merged manually (via git) are detected and skipped. If the session branch exists in multiple plan status files, use `--plan` to disambiguate.

### 7. Monitor progress

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

`--name` and `-b` / `--session-branch` are aliases — both declare the session branch identity. The orchestrator creates the branch from `--base` if it doesn't yet exist, or reuses it on resume.

| Flag | Session branch | Base | Source | Use case |
|------|----------------|------|--------|----------|
| *(default)* | `workbench-N` | `main` | `origin/main` (fetched) | Start from latest remote |
| `--name my-feature` | `my-feature` | `main` | `origin/main` (fetched) | Named session branch (created or reused) |
| `-b my-feature` | `my-feature` | `main` | `origin/main` (fetched) | Same as `--name` |
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
| `planner` | Generates a plan from a prompt or source document (used by `wb plan`) |
| `summarizer` | Extracts requirements from the plan during final review |
| `branch_reviewer` | Reviews the whole session branch against the requirements digest |

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

## Agents

Workbench ships with built-in adapters for Claude Code, Gemini CLI, Codex, Cursor CLI, and Copilot CLI. Use `--agent` to select one:

```bash
wb run plan.md --agent claude     # default
wb run plan.md --agent gemini
wb run plan.md --agent codex
wb run plan.md --agent cursor
wb run plan.md --agent copilot
```

### Custom agents

Define custom adapters via `wb agents add` or by editing `.workbench/agents.yaml` directly:

```bash
wb agents add my-agent --command my-cli --args "--headless,{prompt}" --output-format json
wb run plan.md --agent my-agent
```

This creates an entry in `.workbench/agents.yaml`:

```yaml
agents:
  my-agent:
    command: my-cli
    args: ["--headless", "{prompt}"]
    output_format: json
    json_result_key: result
    json_cost_key: cost_usd
```

The `{prompt}` placeholder in `args` is replaced with the agent's prompt at runtime. Set `output_format: json` to parse structured output with configurable result and cost keys.

### Managing agents

```bash
wb agents init                    # create agents.yaml with all built-in adapter configs
wb agents list                    # show built-in and custom agents
wb agents show my-agent           # show full config for an agent
wb agents add my-agent --command my-cli --args "--headless,{prompt}"
wb agents add my-agent --command new-cli   # update an existing agent
wb agents remove my-agent         # remove a custom agent
```

`wb agents init` creates `.workbench/agents.yaml` pre-populated with the configs for all built-in adapters (Claude, Gemini, Codex, Cursor, Copilot). Use this as a starting point to customize command flags, output parsing, or to add your own agents.

## Directive overrides

Override the instructions given to any agent role:

```bash
wb run plan.md --reviewer-directive "Focus only on security issues."
wb run plan.md --tester-directive "Run pytest with -x flag, fail fast."
```

Available: `--implementor-directive`, `--tester-directive`, `--reviewer-directive`, `--fixer-directive`, `--summarizer-directive`, `--branch-reviewer-directive`.

## CLI reference

### Commands

| Command | Description |
|---|---|
| `wb plan "<prompt>"` | Generate a plan from a natural language description |
| `wb run <plan>` | Execute a plan with parallel agents |
| `wb merge -b <branch>` | Merge completed-but-unmerged task branches (auto-detects plan) |
| `wb final-review <branch>` / `wb review <branch>` | Run a whole-branch review (requirements summarizer + branch reviewer) and optionally open a PR |
| `wb preview <plan>` | Dry-run: show parsed tasks and waves |
| `wb setup` | Create `.workbench/`, install skills, and optionally create a profile |
| `wb status` | Show active worktrees |
| `wb stop` | Kill all running agent tmux sessions |
| `wb clean [project]` | Remove worktrees, branches, and completed-plan status files (scoped to one plan when `project` is given) |
| `wb conventions init` | Create `.workbench/conventions.md` from a template (or from a codebase scan with `--generate`) |
| `wb conventions edit` / `show` / `delete` | Manage `.workbench/conventions.md` |
| `wb agents init` | Create agents.yaml with built-in adapter configs |
| `wb agents list` | List built-in and custom agent adapters |
| `wb agents show <name>` | Show details for an agent adapter |
| `wb agents add <name>` | Add or update a custom agent adapter |
| `wb agents remove <name>` | Remove a custom agent adapter |
| `wb profile init` | Create profile.yaml from defaults |
| `wb profile show` | Show resolved profile |
| `wb profile set <key> <value>` | Update a profile field |
| `wb profile diff` | Show differences from defaults |

### `wb plan`

Takes an optional prompt argument and/or `--from` flag. At least one must be provided.

The planner agent surveys the codebase (project structure, patterns, test infrastructure) and writes a detailed plan to `.workbench/<name>/plan.md`. Use `--from` to transform an existing document into workbench format.

| Flag | Description |
|---|---|
| `--from FILE` | Transform an existing document into workbench plan format |
| `-n NAME` / `--name` | Plan file name (default: `plan`). Produces `.workbench/<name>/plan.md` |
| `--agent CMD` | Agent CLI command (default: `claude`) |
| `--no-tmux` | Run without tmux |
| `--repo PATH` | Repository path (auto-detected if omitted) |

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
| `-b NAME` / `--session-branch` | Session branch name; created from `--base` if missing, reused if it exists. Alias of `--name`. |
| `-w N` / `--wave` | Run only wave N (clamped to valid range) |
| `--start-wave N` | Start from wave N, run through end (default: 1) |
| `--end-wave N` | Stop after wave N (default: last wave) |
| `--retry-failed` | Auto-retry tasks that crashed (not those that exhausted fix retries) |
| `--fail-fast` | Stop after the first wave with any failed tasks |
| `--headroom` / `--no-headroom` | Route supported agents through a local Headroom proxy to reduce token costs |
| `--only-incomplete` | Skip completed tasks from a prior run (requires `-b`) |
| `--task ID` | Run only specific tasks by ID or slug (repeatable) |
| `--cleanup` | Remove worktrees after completion |
| `--keep-branches` | Keep task branches after merging (default: auto-delete on success) |
| `--push` | Push the session branch to origin after merging (sets upstream tracking) |
| `--final-review` | Run a whole-branch review after merges complete; opens a PR on PASS |
| `--pr-title TEXT` | Override the PR title (default: plan H1, then plan id) |
| `--pr-body-file PATH` | Use this file's content as the PR body |
| `--pr-base BRANCH` | Override the PR base branch (default: session's recorded base) |
| `--skip-pr` | Skip PR creation even on PASS verdict |
| `--summarizer-directive TEXT` | Override the requirements summarizer agent's instructions |
| `--branch-reviewer-directive TEXT` | Override the branch reviewer agent's instructions |
| `--repo PATH` | Repository path (auto-detected if omitted) |
| `--profile PATH` | Use a specific profile.yaml |
| `--profile-name NAME` | Use a named profile (`profile.<name>.yaml`) |
| `--*-directive TEXT` | Override instructions for a specific agent role |

Headroom is off by default. Enable it with `--headroom` or a top-level `headroom:` config block in `agents.yaml`; Workbench manages one shared local proxy per run. Claude and Codex are wired first, while other agents run normally.

#### Frontmatter-readable flags

Plans may declare these keys in a YAML frontmatter block (`---` delimiters) at the top of the file. Values act as defaults; explicit CLI flags always win. Unknown keys raise an error.

| Key | CLI flag | Type |
|---|---|---|
| `session_branch` | `-b` / `--session-branch` | string (alias of `name`) |
| `name` | `--name` | string (alias of `session_branch`) |
| `base` | `--base` | string |
| `local` | `--local` | bool |
| `agent` | `--agent` | string |
| `profile` | `--profile` | string (path) |
| `profile_name` | `--profile-name` | string |
| `max_concurrent` | `-j` / `--max-concurrent` | int (>= 1) |
| `max_retries` | `--max-retries` | int (>= 0) |
| `tdd` | `--tdd` | bool |
| `skip_test` | `--skip-test` | bool |
| `skip_review` | `--skip-review` | bool |
| `retry_failed` | `--retry-failed` | bool |
| `fail_fast` | `--fail-fast` | bool |
| `cleanup` | `--cleanup` | bool |
| `keep_branches` | `--keep-branches` | bool |
| `push` | `--push` | bool |
| `final_review` | `--final-review` | bool |

### `wb resume`

Sugar over `wb run <plan> -b <session> --only-incomplete`. Looks up the session in `.workbench/<plan>/status.yaml` (legacy `.workbench/status-*.yaml` is still read), finds the original plan via the recorded `plan_source`, and re-runs every task that isn't `done + merged`. Frontmatter is read from the plan referenced by the session's `plan_source`; same precedence rules as `wb run`.

```bash
wb resume workbench-1
wb resume workbench-1 --tdd          # if the original session was TDD
wb resume workbench-1 --no-tmux
```

| Flag | Description |
|---|---|
| `--no-tmux` | Run agents as subprocesses instead of tmux |
| `--agent CMD` | Agent CLI command (default: `claude`) |
| `-j N` / `--max-concurrent` | Max parallel tasks per wave (default: 4) |
| `--max-retries N` | Max fix attempts after a failed test or review (default: 2) |
| `--tdd` | Run pending tasks in TDD mode |
| `--profile PATH` | Use a specific profile.yaml |
| `--name NAME` | Named profile to resolve |
| `--repo PATH` | Repository path (auto-detected if omitted) |

For finer-grained control (waves, directive overrides, selective tasks), use `wb run` directly.

### `wb setup`

| Flag | Description |
|---|---|
| `--agent NAME` | Target platform: `claude`, `gemini`, `cursor`, `codex`, `copilot`, `manual` (auto-detected if omitted) |
| `--global` | Install skills to user-level paths only (skip `.workbench/` creation) |
| `--symlink` | Symlink instead of copy (stays in sync with package updates) |
| `--profile` | Also create a profile.yaml with the detected agent |
| `--update` | Force-update skills to the latest version |
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb merge`

| Flag | Description |
|---|---|
| `-b NAME` / `--session-branch` | Session branch to merge into (required) |
| `--plan PATH` | Plan file to determine status file (auto-detected if omitted) |
| `--agent CMD` | Agent CLI for conflict resolution (default: `claude`) |
| `--no-tmux` | Run resolver agents as subprocesses instead of tmux |
| `--keep-branches` | Keep task branches after merging |
| `--push` | Push the session branch to origin after merging (sets upstream tracking) |
| `--review` | After merging, run a whole-branch review (and open a PR on PASS) |
| `--pr-title TEXT` | Override the PR title |
| `--pr-body-file PATH` | Use this file's content as the PR body |
| `--pr-base BRANCH` | Override the PR base branch |
| `--summarizer-directive TEXT` | Override the requirements summarizer agent's instructions |
| `--branch-reviewer-directive TEXT` | Override the branch reviewer agent's instructions |
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb final-review` (alias: `wb review`)

Run a whole-branch review for a completed session. Two agents run in sequence: a **requirements summarizer** extracts a structured digest from the plan, then a **branch reviewer** evaluates the session-branch diff against that digest and writes a markdown report. On `VERDICT: PASS`, workbench opens a GitHub PR via `gh pr create`. On `VERDICT: FAIL`, no PR is created; the report lists specific findings with file/line evidence and concrete suggested fixes for a human to address.

Artifacts land under `.workbench/<plan-id>/wrap-up/<session>/`:
- `requirements.md` — the requirements digest
- `review.md` — the review report
- `pr-body.md` — the PR title and body (written when the writer runs)

Two ephemeral worktrees (`.review-wt`, `.pr-writer-wt`) are created inside the same `wrap-up/<session>/` folder while the agents run and are deleted when the phase completes.

Each run appends an entry to the session's `final_reviews` list in `.workbench/<plan-id>/status.yaml`, and `wb status` surfaces the latest verdict and PR URL (or report path on fail).

```bash
wb final-review workbench-1                       # default: open PR on PASS
wb final-review workbench-1 --skip-pr             # never open a PR
wb final-review workbench-1 --pr-title "My feat"  # override PR metadata
```

| Flag | Description |
|---|---|
| `--agent CMD` | Agent CLI command (default: `claude`) |
| `--no-tmux` | Run agents as subprocesses instead of tmux |
| `--pr-title TEXT` | Override the PR title (default: plan H1, else plan id) |
| `--pr-body-file PATH` | Use this file's content as the PR body |
| `--pr-base BRANCH` | Override the PR base branch (default: session's recorded base) |
| `--skip-pr` | Skip PR creation even on PASS verdict |
| `--summarizer-directive TEXT` | Override the requirements summarizer agent's instructions |
| `--branch-reviewer-directive TEXT` | Override the branch reviewer agent's instructions |
| `--profile PATH` | Use a specific profile.yaml |
| `--profile-name NAME` | Use a named profile |
| `--repo PATH` | Repository path (auto-detected if omitted) |

The summarizer and branch_reviewer roles are configurable in `profile.yaml` like any other role (`agent`, `directive`).

`gh` must be installed and authenticated for PR creation. When it isn't, the review still runs and writes its report; the PR step prints a copy-pasteable `gh pr create` command instead of failing.

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

Removes workbench worktrees, `wb/*` branches, and completed-plan status files. Default mode refuses if any in-flight artifacts exist; pass `--completed` to skip them silently or `--force` to wipe everything.

```bash
wb clean                                # remove only fully-completed plans
wb clean my-plan                        # scope to one plan; also removes the empty .workbench/my-plan/ folder
wb clean .workbench/my-plan/plan.md     # same — path form is accepted too
wb clean --completed                    # skip in-flight artifacts without erroring
wb clean --force                        # remove everything, including in-flight
wb clean --remove-plans                 # also delete the plan source markdown
wb clean --dry-run                      # preview what would be removed
```

| Flag / argument | Description |
|---|---|
| `PROJECT` (positional, optional) | Plan name or path to a `plan.md` (e.g. `my-plan` or `.workbench/my-plan/plan.md`). Scopes cleanup to that plan only and removes the folder when empty. |
| `--force` | Remove all worktrees and `wb/*` branches, including in-flight ones. Mutually exclusive with `--completed`. |
| `--completed` | Only remove artifacts for completed plans; skip in-flight ones silently. |
| `--remove-plans` | Also delete plan source markdown for completed plans. |
| `--dry-run` | Print what would be removed without removing anything. |
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb conventions`

Manages the optional project-wide conventions file at `.workbench/conventions.md`. When a plan lacks its own `## Conventions` section, workbench injects this file's content into agent prompts at runtime (orchestrator, summarizer, branch reviewer, PR writer). The planner is also conventions-aware: it sees the file's content when generating a plan and skips writing a duplicate `## Conventions` section.

```bash
wb conventions init               # write a starter template
wb conventions init --generate    # dispatch an agent (generate-conventions skill) to draft from the codebase
wb conventions edit               # open in $EDITOR (creates from template if missing)
wb conventions show               # print to stdout
wb conventions delete             # remove the file (prompts unless --yes)
```

`init` errors if the file already exists; "redo from scratch" is `wb conventions delete && wb conventions init --generate`.

| Subcommand | Flag | Description |
|---|---|---|
| `init` | `--generate` | Drafts the file via an agent instead of writing a static template |
| `init` | `--agent CMD` | Agent CLI for `--generate` (default: `claude`) |
| `init` | `--no-tmux` | Run `--generate` without tmux |
| `delete` | `--yes` | Skip confirmation prompt |
| all | `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb agents init`

| Flag | Description |
|---|---|
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb agents list`

| Flag | Description |
|---|---|
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb agents show`

Takes a single argument: the agent name.

| Flag | Description |
|---|---|
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb agents add`

Takes a single argument: the agent name.

| Flag | Description |
|---|---|
| `--command CMD` | CLI command to invoke (required) |
| `--args TEMPLATE` | Argument template, comma-separated (default: `{prompt}`) |
| `--output-format FMT` | `text` or `json` (default: `text`) |
| `--json-result-key KEY` | JSON key for result (default: `result`) |
| `--json-cost-key KEY` | JSON key for cost (default: `cost_usd`) |
| `--repo PATH` | Repository path (auto-detected if omitted) |

### `wb agents remove`

Takes a single argument: the agent name.

| Flag | Description |
|---|---|
| `--repo PATH` | Repository path (auto-detected if omitted) |

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
