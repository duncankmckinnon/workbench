---
name: use-workbench
description: Use when running, resuming, or troubleshooting `wb` execution of a workbench plan — pipeline stages, failure recovery, wave control, branching strategy, profiles, and TDD mode. For writing or editing the plan file itself, use the plan-workbench skill.
---

# Running Workbench Plans

How to run, resume, and troubleshoot the `wb` CLI once a plan already exists.

**To write or edit a plan file (`.workbench/<name>/plan.md`), use the `plan-workbench` skill instead** — it covers plan structure, frontmatter, breaking directives into tasks, conventions handling, and testing directives. This skill picks up from there: executing that plan, handling failures, and configuring how agents run.

## Overview

Workbench (`wb`) is a multi-agent orchestrator that takes a markdown plan, breaks it into independent tasks, and dispatches parallel AI coding agents (Claude Code, Google Antigravity, OpenCode, Codex, Cursor CLI, Copilot CLI, or custom) to implement, test, and review each task in isolated git worktrees.

## When to Use

- Running a plan with `wb run`
- Resuming a session, retrying failed tasks, or re-running specific tasks/waves
- Configuring which agent CLI runs each pipeline role (profiles)
- Understanding TDD mode, branching behavior, or merge/failure handling
- Debugging why an agent session failed or produced incorrect output for reasons unrelated to plan clarity (see `plan-workbench` if the plan itself is the problem)

## Agent Pipeline

Each task goes through: **implement -> test -> review -> fix**

- **Implementor** — Writes code and commits to the task branch
- **Tester** — Runs tests, writes new tests if specified, emits `VERDICT: PASS` or `VERDICT: FAIL`
- **Reviewer** — Reviews the diff for correctness and quality, emits a verdict
- **Fixer** — If test/review fails, receives feedback and makes targeted fixes (up to `--max-retries`)
- **Merger** — If merge conflicts occur between parallel branches, resolves them automatically

Stages can be skipped with `--skip-test` or `--skip-review`.

Task outcomes are tracked in `.workbench/status.json` as each task completes, enabling resume-from-failure workflows.

## Handling Failures

### Automatic retry

```bash
wb run plan.md --retry-failed
```

Re-runs tasks that crashed (agent error, timeout) after each wave. Tasks that exhausted their fix retries (`fix_count >= max_retries`) are left alone — they need plan or directive changes, not another blind run.

### Fail fast

```bash
wb run plan.md --fail-fast
```

Stops after the first wave with any failed tasks. Composes with `--retry-failed` (retry first, then stop if still failing).

### Re-run only failed tasks

```bash
wb run plan.md -b workbench-1 --only-incomplete
```

Reads `.workbench/status.json` to skip tasks that already completed. Requires `-b` to specify the session branch to resume.

### Re-run specific tasks

```bash
wb run plan.md -b workbench-1 --task task-2
wb run plan.md -b workbench-1 --task task-1 --task task-3
wb run plan.md -b workbench-1 --task my-feature-name    # by slug
```

Runs only the specified tasks. All other tasks are left untouched — no worktrees, no pipelines, no status changes. Accepts task IDs or slugs (title converted to lowercase-dashes). If a task has an existing branch from a prior run, it is cleaned up and started fresh. Status records for non-targeted tasks are preserved.

`--task` works without `-b` too — it just creates a new session with only those tasks.

### Wave control

Run a specific wave or range of waves instead of the full plan:

```bash
wb run plan.md -w 2                          # run only wave 2
wb run plan.md --start-wave 2                # run waves 2 through end
wb run plan.md --start-wave 2 --end-wave 4   # run waves 2, 3, and 4
wb run plan.md -b workbench-1 -w 3           # resume session, run only wave 3
```

- `-w N` / `--wave N` — run only wave N (sets both start and end)
- `--start-wave N` — start from wave N, run through the last wave (default: 1)
- `--end-wave N` — stop after wave N (default: last wave)

Out-of-range values are clamped automatically with a warning: `--start-wave` defaults to 1, `--end-wave` defaults to the last wave. If `--end-wave` is less than `--start-wave`, it defaults to the last wave.

Waves before `--start-wave` are marked as already completed (skipped). Waves after `--end-wave` are not executed.

### Merge unmerged branches

```bash
wb merge -b workbench-1
```

Merges completed-but-unmerged task branches without re-running pipelines. Uses a resolver agent for conflicts. Branches already merged via git are detected and skipped.

## Directive Overrides

The instructions given to each agent role can be overridden from the CLI:

```bash
wb run plan.md --reviewer-directive "Focus only on security vulnerabilities and data validation."
wb run plan.md --tester-directive "Run pytest with -x flag. Only test the new code, not existing tests."
```

This is useful when you want agents to focus on specific aspects without modifying the plan itself.

## Branching Strategy

By default, `wb run` fetches `origin/main` and creates a new session branch (`workbench-N`) from the latest remote state. This ensures work starts from the most up-to-date code and avoids merge conflicts when the session branch is later merged back.

### Flags

| Flag | Base branch | Source | Use case |
|------|-------------|--------|----------|
| *(default)* | `main` | `origin/main` (fetched) | Standard — start from latest remote |
| `--local` | `main` | local `main` | Build on uncommitted/unpushed local work |
| `--base feature-x` | `feature-x` | `origin/feature-x` (fetched) | Branch from a specific remote branch |
| `--base feature-x --local` | `feature-x` | local `feature-x` | Branch from a local feature branch |
| `-b workbench-3` | *(existing)* | *(existing)* | Resume a previous session branch |

### When to use `--local`

Use `--local` when your base branch has local commits you haven't pushed yet and you want workbench to build on top of them. Without `--local`, workbench fetches from origin and your unpushed work won't be included.

### When to use `--base`

Use `--base` when you're working off a branch other than `main` — for example, a long-running feature branch, a release branch, or another team member's branch. Combined with `--local`, it lets you build on any local branch.

### Resuming with `-b`

Use `-b workbench-N` (or `--session-branch`) to resume a previous session. This skips branch creation entirely and continues merging into the existing session branch. Pair with `-w N` to run only a specific wave, `--start-wave N` to skip already-completed waves, or `--start-wave N --end-wave M` to run a range of waves.

## Profiles

Profiles configure which agent CLI and instructions are used for each pipeline role. When no profile exists, built-in defaults apply.

### Roles and fields

Roles: `implementor`, `tester`, `reviewer`, `fixer`, `merger`

Each role supports:
- `agent` — CLI command (default: `claude`). Supported: `claude`, `antigravity`, `opencode`, `codex`, `cursor`, `copilot`, or any custom CLI via `.workbench/agents.yaml`.
- `directive` — Full replacement for the role's default instructions.
- `directive_extend` — Text appended to the default instructions. Cannot be combined with `directive` on the same role.

### YAML format

Create or edit `.workbench/profile.yaml`:

```yaml
roles:
  reviewer:
    agent: antigravity
    directive: "Focus on security and correctness."
  tester:
    directive_extend: "Also check edge cases for null inputs."
  implementor:
    agent: codex
```

Only include roles and fields you want to override — everything else uses built-in defaults.

### Named profiles

Store multiple configurations as `profile.<name>.yaml`:

```bash
wb profile init --name fast --set reviewer.agent=antigravity --set implementor.agent=codex
wb run plan.md --profile-name fast
```

### Profile CLI commands

```bash
wb profile init                                        # create profile.yaml from defaults
wb profile init --global                               # create in ~/.workbench/
wb profile init --set reviewer.agent=antigravity       # create with inline overrides
wb profile init --name fast --set reviewer.agent=antigravity  # create a named profile
wb profile show                                        # print resolved profile
wb profile show --name fast                            # show a named profile
wb profile set reviewer.agent antigravity              # update a field
wb profile set reviewer.agent codex --name fast        # update a named profile
wb profile diff                                        # show differences from defaults
wb profile diff --name fast                            # diff a named profile
```

### Merge order

Profiles merge in order: built-in defaults < `~/.workbench/profile.yaml` < `.workbench/profile.yaml` < `--profile` flag < CLI flags. Named profiles (`--profile-name`) replace the default filename at each level.

## TDD Mode

With `--tdd`, the pipeline becomes: **test (write failing) → implement (make pass) → test (verify) → review → fix**

In TDD mode, the tester writes comprehensive failing tests first. The implementor then writes code to make all tests pass and reports whether the tests are comprehensive. Normal test verification and review follow.

## Updating workbench

To update workbench and its skills to the latest version:

```bash
pip install --upgrade wbcli    # upgrade the package
wb setup --update              # overwrite project-level skill files with the latest version
```

For user-level skills:

```bash
wb setup --global --update     # update user-level skills
```

If using `--symlink`, skill files stay in sync automatically — no `--update` needed.

## Key commands

- `wb run <plan>` — execute a plan; CLI flags override frontmatter declared at the top of the plan
- `wb run plan.md --name auth-feature` — name the session branch
- `wb run plan.md --keep-branches` — keep task branches after merging
- `wb run plan.md --tdd` — test-driven: tests first, then implement
- `wb run plan.md --base feature-x` — branch from a specific branch
- `wb run plan.md --local` — branch from local ref instead of fetching
- `wb run plan.md -w 2` — run only wave 2
- `wb run plan.md --start-wave 2 --end-wave 4` — run waves 2 through 4
- `wb run plan.md -b my-session -w 3` — resume session, run only wave 3
- `wb run plan.md --profile-name fast` — use a named profile
- `wb run plan.md --retry-failed` — auto-retry crashed tasks
- `wb run plan.md --fail-fast` — stop on first wave failure
- `wb resume workbench-1` — resume a session, re-running every task that isn't done + merged
- `wb run plan.md -b workbench-1 --only-incomplete` — same as `wb resume`, but lets you override flags
- `wb run plan.md -b workbench-1 --task task-2` — re-run a specific task
- `wb merge -b workbench-1` — merge unmerged branches without re-running
- `wb preview <plan>` — dry-run to see parsed tasks and waves
- `wb status` — show active worktrees
- `wb stop` — kill all active agent sessions
- `wb stop --cleanup` — also remove worktrees and branches
- `wb clean` — remove worktrees, `wb/*` branches, and completed-plan status files (refuses if anything is in-flight; pass `--force` or `--completed`)
- `wb clean <project>` — scope cleanup to a single plan; accepts a plan name (`my-plan`) or path (`.workbench/my-plan/plan.md`). Also removes `.workbench/<project>/` if it ends up empty
- `wb clean --dry-run` — preview what would be removed
- `wb conventions init [--generate]` — create `.workbench/conventions.md` from a template (or from a codebase scan with `--generate`)
- `wb conventions edit` / `wb conventions show` / `wb conventions delete` — manage the conventions file
- `wb setup` — create .workbench/, install skills locally, prepare repo
- `wb setup --agent antigravity` — install skills for Google Antigravity CLI
- `wb setup --agent opencode` — install skills for OpenCode CLI
- `wb setup --profile` — also create a profile.yaml with the detected agent
- `wb setup --update` — force-update skills to the latest version
- `wb setup --global` — install skills to user-level paths (no .workbench/ creation)
- `wb setup --global --agent claude` — install to ~/.claude/skills/
- `wb setup --global --agent antigravity` — install to ~/.agents/skills/
- `wb setup --global --agent opencode` — install to ~/.agents/skills/
- `wb agents init` — create agents.yaml with built-in adapter configs
- `wb agents list` — show built-in and custom agents
- `wb agents add <name> --command <cmd>` — add a custom agent
- `wb agents remove <name>` — remove a custom agent
- `wb profile init` — create profile.yaml from defaults
- `wb profile init --name fast --set reviewer.agent=antigravity` — create a named profile with overrides
- `wb profile show` — print resolved profile
- `wb profile set <key> <value>` — update a profile field
- `wb profile diff` — show differences from defaults
