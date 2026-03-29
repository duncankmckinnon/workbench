---
name: use-workbench
description: Use when writing or editing a workbench plan (.workbench/*.md), designing task graphs for parallel agent execution, or preparing work for the wb CLI to dispatch
---

# Writing Workbench Plans

How to write effective plans for the `wb` CLI to execute with parallel AI agents.

## Overview

Workbench (`wb`) is a multi-agent orchestrator that takes a markdown plan, breaks it into independent tasks, and dispatches parallel AI coding agents to implement, test, and review each task in isolated git worktrees.

Each task becomes a standalone agent session — the agent only sees its own task description, not the rest of the plan. This means the plan must be thorough enough that each task is self-sufficient.

## When to Use

- Writing a new `.workbench/*.md` plan file
- Breaking a feature or refactor into parallel agent tasks
- Reviewing whether a plan will execute correctly before running `wb run`
- Debugging why agents produced incorrect output (usually a plan clarity issue)

## Plan Format

```markdown
# Plan Title

## Context

<What is this project? What are we building? Why?>

<Key architectural decisions and constraints>

## Conventions

<Project-specific patterns agents must follow:>
- <Language/framework version>
- <Import conventions>
- <Error handling patterns>
- <Naming conventions>
- <Test patterns and test command>

## Task: Short title
Files: src/auth.py, src/middleware.py
Depends: database-setup

Detailed description of what to implement...

### Expected behavior
<Concrete specification of what the code should do>

### Test plan
<What tests to write, what command to run, what passing looks like>
```

### Plan Sections

- `## Context` — Injected into every agent's prompt. Describe the project, what's being built, and why.
- `## Conventions` — Injected into every agent's prompt. Specify language version, test framework, import style, naming conventions. Without this, agents follow their own defaults.
- `## Task: <title>` — Each task becomes an independent agent session in its own git worktree.

### Task Metadata

- **Files:** — Comma-separated list of files the task creates or modifies. Prevents parallel tasks from conflicting.
- **Depends:** — Comma-separated task slugs this task depends on. Tasks with unmet dependencies wait until earlier waves complete.
- Aliases: `Scope:` works like `Files:`, `After:`/`Dependencies:` work like `Depends:`.

### Dependency Slugs

Dependencies reference other tasks by their title converted to a slug (lowercase, non-alphanumeric replaced with `-`). For example, `## Task: Database Setup` has slug `database-setup`.

**Keep task titles short (2-4 words).** The title becomes the dependency slug, and long titles produce unwieldy slugs that are error-prone to type in `Depends:` lines. Compare:

| Title | Slug | Verdict |
|-------|------|---------|
| Prompt builder | `prompt-builder` | Good |
| Agent adapters | `agent-adapters` | Good |
| Structured prompt builder with plan context injection | `structured-prompt-builder-with-plan-context-injection` | Too long |
| Update agents to use adapters and tmux | `update-agents-to-use-adapters-and-tmux` | Too long |

Treat the title as a label, not a description — the task body has all the detail.

## Writing Good Task Descriptions

Each task runs in an **isolated worktree** — the agent only sees its own task description, not other tasks. Every task description must contain:

1. **What to build** — Concrete deliverables, not vague goals
2. **Where it goes** — Exact file paths for new and modified files
3. **How it works** — Function signatures, type definitions, behavior specs
4. **How it fits** — Imports, interfaces with existing code, how other modules will use this
5. **Patterns to follow** — "Use the same pattern as X" with enough detail to follow it
6. **Test expectations** — What tests to write, what command to run, what passing looks like
7. **Edge cases** — Error handling, boundary conditions, validation rules
8. **What NOT to do** — Constraints, anti-patterns, things that seem obvious but are wrong in this codebase

If the task depends on interfaces from an earlier wave, describe those interfaces in full — the agent cannot see the other task's output.

### Example: Good vs Bad

**Bad** (too vague, agent will guess):
```markdown
## Task: Add authentication
Add auth to the app.
```

**Good** (self-contained, specific):
```markdown
## Task: JWT auth middleware
Files: src/auth/middleware.py, src/auth/tokens.py, tests/test_auth.py

Create JWT-based authentication middleware for the FastAPI app.

### Implementation
- `src/auth/tokens.py`: `create_token(user_id: str) -> str` and `verify_token(token: str) -> dict`
  using PyJWT. Tokens expire after 24h. Secret from `AUTH_SECRET` env var.
- `src/auth/middleware.py`: FastAPI dependency `require_auth(request: Request) -> User`
  that extracts Bearer token from Authorization header, verifies it, and returns the user.
- Follow the existing middleware pattern in `src/middleware/logging.py`.

### Tests
- `tests/test_auth.py`: test token creation, verification, expiry, and invalid tokens.
- Run: `pytest tests/test_auth.py`
```

## Designing for Parallelism

Tasks in the same wave run simultaneously in separate worktrees. They **cannot see each other's changes**, and modifying the same files causes merge conflicts.

**Strategies:**
- Group work by file ownership — each task owns distinct files
- Push shared infrastructure (types, configs) to earlier waves using `Depends:`
- If two tasks must touch the same file, make one depend on the other

### Example: Parallel-Safe Plan

```markdown
## Task: User model
Files: src/models/user.py, migrations/001_users.sql

## Task: Product model
Files: src/models/product.py, migrations/002_products.sql

## Task: API endpoints
Files: src/api/routes.py, src/api/handlers.py
Depends: user-model, product-model
```

Wave 1 runs the two model tasks in parallel (different files). Wave 2 runs the API task after both complete.

## Planning Process

Creating a good plan is the most important step. Follow these phases:

### Phase 1: Understand the Problem
- What is the user trying to achieve? What's the end state?
- What are the constraints (performance, compatibility, existing patterns)?
- What's changing? What's staying the same?

Ask clarifying questions if anything is ambiguous. It's better to ask now than to have 6 agents each make a different assumption.

### Phase 2: Survey the Codebase
Read the code before designing tasks:
- Project structure, module organization, entry points
- Existing patterns — how are similar things already done?
- Dependencies and interfaces between modules
- Test infrastructure — framework, location, test command
- Build and config files

### Phase 3: Design the Task Graph
Break work into tasks with the execution model in mind:
- Each task runs in an isolated worktree
- File overlap between parallel tasks creates merge conflicts
- Push shared infrastructure to earlier waves
- Maximize parallelism by grouping work by file ownership

### Phase 4: Write Detailed Descriptions
Follow the checklist in "Writing Good Task Descriptions" above. Every task must include enough context for an agent that has never seen the rest of the plan.

### Phase 5: Validate
Before running:
- [ ] Can each task be implemented knowing only its own description?
- [ ] Are file sets disjoint within each wave?
- [ ] Do dependent tasks describe the interfaces they depend on?
- [ ] Is the test command specified and will it work in a fresh worktree?
- [ ] Are there implicit assumptions that should be explicit?

## Agent Pipeline

Each task goes through: **implement -> test -> review -> fix**

- **Implementor** — Writes code and commits to the task branch
- **Tester** — Runs tests, writes new tests if specified, emits `VERDICT: PASS` or `VERDICT: FAIL`
- **Reviewer** — Reviews the diff for correctness and quality, emits a verdict
- **Fixer** — If test/review fails, receives feedback and makes targeted fixes (up to `--max-retries`)
- **Merger** — If merge conflicts occur between parallel branches, resolves them automatically

Stages can be skipped with `--skip-test` or `--skip-review`.

## Directive Overrides

The instructions given to each agent role can be overridden from the CLI:

```bash
wb run plan.md --reviewer-directive "Focus only on security vulnerabilities and data validation."
wb run plan.md --tester-directive "Run pytest with -x flag. Only test the new code, not existing tests."
```

This is useful when you want agents to focus on specific aspects without modifying the plan itself.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Task says "add auth" with no details | Specify exact files, function signatures, error handling, test command |
| Two parallel tasks edit the same file | Add `Depends:` to serialize them, or extract shared changes to an earlier task |
| Task depends on another but doesn't describe the interface | Copy function signatures and types into the dependent task's description |
| No `## Context` or `## Conventions` section | Agents follow their own defaults — specify language version, test framework, import style |
| Test command missing or wrong | Agent may skip tests or run the wrong suite — always include `Run: <command>` |
| Task title is a full sentence | Keep titles to 2-4 words — they become dependency slugs |
| Line number references for code to change | Line numbers shift — describe code by content/pattern instead |

## CLI Reference

With `--tdd`, the pipeline becomes: **test (write failing) → implement (make pass) → test (verify) → review → fix**

In TDD mode, the tester writes comprehensive failing tests first. The implementor then writes code to make all tests pass. Normal test verification and review follow.

## Key commands

- `wb run <plan>` — execute a plan with parallel agents
- `wb preview <plan>` — dry-run to see parsed tasks and waves
- `wb status` — show active worktrees
- `wb run plan.md --tdd` — test-driven: tests first, then implement
- `wb stop` — kill all active agent sessions
- `wb stop --cleanup` — also remove worktrees and branches
- `wb clean` — remove all workbench worktrees
- `wb setup` — prepare a repo for workbench use
- `wb init` — install workbench skills for your agent platform
