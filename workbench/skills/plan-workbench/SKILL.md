---
name: plan-workbench
description: Use when writing or editing a workbench plan (.workbench/<name>/plan.md) — plan structure, frontmatter, breaking directives into independent tasks tied to specific files, conventions handling, and testing directives
---

# Writing Workbench Plans

How to write a `.workbench/<name>/plan.md` that the `wb` CLI can break into independent tasks and dispatch to parallel AI agents.

To run a plan you've written, or for pipeline/failure/branching/profile mechanics, see the `use-workbench` skill instead — this skill is only about authoring the plan file.

## Overview

Workbench (`wb`) takes a markdown plan and breaks it into tasks, each dispatched to a separate agent in its own isolated git worktree. **Each task's agent only sees its own task description** — not the rest of the plan, not other tasks' output. This one fact drives every rule below: a plan is only as good as how self-sufficient each task is in isolation.

## When to Use

- Writing a new `.workbench/<name>/plan.md`
- Breaking a feature, refactor, or directive into parallel agent tasks
- Reviewing whether a plan will execute correctly before `wb run`
- Debugging why agents produced incorrect output (usually a plan clarity issue)

## Plan File Location

Plans always live at `.workbench/<plan-name>/plan.md` — a named subdirectory, file always called `plan.md`. Never write a flat `.workbench/<plan-name>.md`.

```
.workbench/auth-rewrite/plan.md
.workbench/build-site/plan.md
```

This is the layout the `wb` CLI expects (its own output reports "Parsed N task(s) from .workbench/<name>/plan.md"), and the directory becomes the namespace for plan-adjacent files the CLI writes during execution (logs, status). Run with `wb run .workbench/<name>/plan.md`.

## Plan Format

```markdown
---
name: <plan-name>
---
# Plan Title

## Context

<What is this project? What are we building? Why?>

<Key architectural decisions and constraints>

## Conventions

<Project-specific patterns agents must follow — omit this section entirely if
.workbench/conventions.md already covers it; see "Conventions Handling" below>

## Task: Short title
Files: src/auth.py, src/middleware.py
Depends: database-setup

Detailed description of what to implement...

### Expected behavior
<Concrete specification of what the code should do>

### Test plan
<What tests to write, what command to run, what passing looks like>
```

## Plan run-config (frontmatter)

Every plan **must** open with a `---`-delimited YAML block before `# Title`, declaring at least `name:` (or its alias `session_branch:`) — the plan/session identity. Add any other run-config keys only when the plan deliberately wants a non-default value; omit keys left at their defaults.

```markdown
---
session_branch: workbench-auth
base: feature-auth
tdd: true
max_concurrent: 6
---
# Auth refactor
```

**Why this is non-negotiable:** frontmatter run-config (`plan.run_config`) is parsed by `workbench/plan_parser.py` against `_RUN_CONFIG_SCHEMA` and travels the plan's run settings with the file itself, instead of relying on someone remembering the right CLI flags.

### Precedence

CLI flag > frontmatter > built-in default, determined via `click.Context.get_parameter_source()`.

### Schema

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

Unknown keys raise `ValueError` — typos are never silently ignored.

### Session branch semantics

`session_branch` and `name` are aliases — both declare the session branch identity for this plan. The orchestrator creates the branch from `base` (default `main`) if it doesn't exist, reuses it if it does (so re-running resumes the same session), and auto-numbers a new `workbench-<N>` branch when neither field is set. Set both only by mistake; if they differ, `session_branch` wins with a warning.

### Not allowed in frontmatter

`--repo`, `--no-tmux`, `-w`/`--start-wave`/`--end-wave`, `--task`, `--only-incomplete`, and `*-directive` flags are per-invocation, not plan-shaped — they stay CLI-only.

### Plan Sections

- `## Context` — injected into every agent's prompt. Describe the project, what's being built, and why.
- `## Conventions` — injected into every agent's prompt (see "Conventions Handling" below).
- `## Task: <title>` — each becomes an independent agent session in its own worktree.

## Conventions Handling

A repo can ship one canonical conventions file at `.workbench/conventions.md` instead of every plan repeating the same rules. Resolution is fallback-only, at runtime, and never mutates the plan file:

| Plan has `## Conventions`? | `.workbench/conventions.md` exists? | What agents see |
|---|---|---|
| Yes | either | The plan's own section. The file is ignored. |
| No | Yes | The file's content, injected as a `## Conventions` section at runtime. |
| No | No | No conventions section. Agents follow their own defaults. |

**When writing a plan, decide up front:**
1. Check whether `.workbench/conventions.md` exists (`wb conventions show`). If it exists and covers this plan's stack, **omit `## Conventions` entirely** — don't copy its content into the plan, and don't write a redundant section that could drift out of sync.
2. If it doesn't exist and the source material implies conventions worth capturing (language/framework version, test command, import style, naming), write a `## Conventions` section in the plan itself.
3. If the repo has no conventions file yet and conventions are going to matter across multiple future plans (not just this one), suggest `wb conventions init --generate` instead of duplicating rules plan-by-plan — that dispatches the `generate-conventions` skill to draft the file from a codebase scan.

Never invent conventions you can't trace back to the repo (lint configs, existing code, README/CONTRIBUTING). If a plan's stack genuinely differs from the rest of the repo (e.g., one plan touches a subproject in a different language), its own `## Conventions` section should override the shared file for that plan.

## Task Metadata

- **Files:** — comma-separated list of files the task creates or modifies. Prevents parallel tasks from conflicting.
- **Depends:** — comma-separated task slugs this task depends on. Tasks with unmet dependencies wait until earlier waves complete.
- Aliases: `Scope:` works like `Files:`; `After:`/`Dependencies:` work like `Depends:`.

### Dependency slugs

Dependencies reference other tasks by their title converted to a slug (lowercase, non-alphanumeric replaced with `-`). `## Task: Database Setup` has slug `database-setup`.

**Keep task titles short (2-4 words)** — the title becomes the dependency slug, and long titles produce unwieldy, error-prone slugs:

| Title | Slug | Verdict |
|-------|------|---------|
| Prompt builder | `prompt-builder` | Good |
| Agent adapters | `agent-adapters` | Good |
| Structured prompt builder with plan context injection | `structured-prompt-builder-with-plan-context-injection` | Too long |

Treat the title as a label, not a description — the task body carries the detail.

## Breaking a Directive into Independent Tasks

The core planning skill is turning "build X" into a set of tasks that can run in parallel without stepping on each other. Tasks in the same wave run simultaneously in separate worktrees: **they cannot see each other's changes**, and two tasks touching the same file will conflict.

**Method:**
1. List every file the directive touches (new and modified).
2. Group files by natural ownership — a model, an endpoint, a config, a migration. Each group is a candidate task.
3. If two groups must touch the same file, either merge them into one task or add `Depends:` to serialize them.
4. Push shared infrastructure (types, schemas, config) that multiple tasks need into its own earlier-wave task; later tasks `Depends:` on it.
5. Within a wave, verify file sets are fully disjoint before finalizing.

### Example: parallel-safe decomposition

```markdown
## Task: User model
Files: src/models/user.py, migrations/001_users.sql

## Task: Product model
Files: src/models/product.py, migrations/002_products.sql

## Task: API endpoints
Files: src/api/routes.py, src/api/handlers.py
Depends: user-model, product-model
```

Wave 1 runs the two model tasks in parallel (disjoint files). Wave 2 runs the API task once both are merged.

## Writing Self-Contained Task Descriptions

Because each task's agent sees only its own description, every task must contain:

1. **What to build** — concrete deliverables, not vague goals
2. **Where it goes** — exact file paths for new and modified files
3. **How it works** — function signatures, type definitions, behavior specs
4. **How it fits** — imports, interfaces with existing code, how other modules will use this
5. **Patterns to follow** — "use the same pattern as X" with enough detail to actually follow it
6. **Test expectations** — what to test, what command to run, what passing looks like (or an explicit skip — see Testing Directives)
7. **Edge cases** — error handling, boundary conditions, validation rules
8. **What NOT to do** — constraints, anti-patterns, things that seem obvious but are wrong in this codebase

If a task depends on interfaces from an earlier wave, describe those interfaces in full (signatures, types) in the task body — the agent cannot see the other task's code.

## Testing Directives

Every task goes through a tester stage by default. Most tasks change executable behavior and need a real test command — always include a concrete `Run: <command>` and what passing looks like.

**But some tasks produce no executable behavior to verify** — pure markdown (skill files, docs, READMEs), static config with no runtime logic, or content-only changes. For these, don't invent a test just to give the tester something to run — a "file exists" check is busywork that fights the pipeline rather than removing the stage it doesn't need. Instead, write an explicit directive in the task body:

```markdown
### Test plan
Tester agent: emit `VERDICT: PASS` immediately without running any test command.
This task ships only [a SKILL.md file / documentation / static config] with no executable behavior to verify.
```

The reviewer stage still runs normally — this only short-circuits testing.

**Rule of thumb:** if you can't articulate what command would meaningfully fail, the task doesn't need a test command — it needs this directive instead.

## Planning Process

### Phase 1: Understand the directive
What's the end state? What are the constraints (performance, compatibility, existing patterns)? What's changing vs. staying the same? Ask clarifying questions before designing tasks — it's cheaper than 6 agents each guessing differently.

### Phase 2: Survey the codebase
Read before designing: project structure and entry points, existing patterns for similar work, dependencies/interfaces between modules, test infrastructure (framework, location, run command), build/config files, and whether `.workbench/conventions.md` already exists.

### Phase 3: Design the task graph
Apply "Breaking a Directive into Independent Tasks" above: group by file ownership, push shared infra to earlier waves, maximize parallelism, verify disjoint file sets per wave.

### Phase 4: Write detailed descriptions
Follow the "Writing Self-Contained Task Descriptions" checklist for every task. Add explicit testing directives per "Testing Directives" for any non-code task.

### Phase 5: Validate before running
- [ ] Frontmatter present with at least `name:`
- [ ] Plan file lives at `.workbench/<name>/plan.md`
- [ ] Can each task be implemented knowing only its own description?
- [ ] Are file sets disjoint within each wave?
- [ ] Do dependent tasks describe the interfaces they depend on?
- [ ] Does every code-changing task specify a concrete test command that will work in a fresh worktree?
- [ ] Does every non-code task carry an explicit tester PASS directive instead of a fabricated test?
- [ ] Is `## Conventions` present only when `.workbench/conventions.md` doesn't already cover it?
- [ ] Are there implicit assumptions that should be made explicit?

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| No frontmatter, or missing `name:` | Every plan opens with a `---` block declaring at least `name:` |
| Task says "add auth" with no details | Specify exact files, function signatures, error handling, test command |
| Two parallel tasks edit the same file | Add `Depends:` to serialize them, or extract shared changes to an earlier task |
| Task depends on another but doesn't describe the interface | Copy function signatures and types into the dependent task's description |
| No `## Context` or `## Conventions`, and no `.workbench/conventions.md` | Agents follow their own defaults — specify language version, test framework, import style |
| Test command missing or wrong for a code task | Agent may skip tests or run the wrong suite — always include `Run: <command>` |
| Fabricated "file exists" test for a skill/docs-only task | Use the explicit tester PASS directive instead — don't invent tests with nothing to verify |
| `## Conventions` duplicates `.workbench/conventions.md` | Omit the plan section and let the fallback inject the file — one source of truth |
| Task title is a full sentence | Keep titles to 2-4 words — they become dependency slugs |
| Line number references for code to change | Line numbers shift — describe code by content/pattern instead |
| Flat `.workbench/<name>.md` file | Plans live at `.workbench/<name>/plan.md`, in a named subdirectory |
