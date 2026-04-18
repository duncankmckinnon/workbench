# Workbench Plan Writing Guide

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

**Keep task titles short (2-4 words).** The title becomes the dependency slug, and long titles produce unwieldy slugs.

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

## Agent Pipeline

Each task goes through: **implement -> test -> review -> fix**

- **Implementor** — Writes code and commits to the task branch
- **Tester** — Runs tests, writes new tests if specified, emits `VERDICT: PASS` or `VERDICT: FAIL`
- **Reviewer** — Reviews the diff for correctness and quality, emits a verdict
- **Fixer** — If test/review fails, receives feedback and makes targeted fixes (up to `--max-retries`)
- **Merger** — If merge conflicts occur between parallel branches, resolves them automatically

Stages can be skipped with `--skip-test` or `--skip-review`.
