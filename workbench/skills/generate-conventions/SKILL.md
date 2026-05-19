---
name: generate-conventions
description: Use when generating a project conventions file at .workbench/conventions.md by scanning the codebase. Invoked by `wb conventions init --generate`.
---

# Generate project conventions

How to draft a `.workbench/conventions.md` for a repo by reading what the project already encodes.

## When to use

- The user ran `wb conventions init --generate`, OR
- The user explicitly asked you to create or refresh `.workbench/conventions.md`

## What to do

1. **Survey** the repo (read-only, no edits):
   - `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` at the repo root if present — these often encode conventions explicitly.
   - Lint/format configs: `pyproject.toml` (tool.ruff, tool.black, tool.mypy), `.eslintrc*`, `.prettierrc`, `ruff.toml`, `tsconfig.json`, language-appropriate equivalents.
   - `README.md` and `CONTRIBUTING.md` for stated practices.
   - Sample ~5 representative source files to confirm what the configs actually produce in practice (line length, import order, naming).
   - One or two test files to derive the test framework, naming convention, and run command.
2. **Synthesize** a draft using the section structure below. Every bullet must be:
   - **Short** — one line, no prose.
   - **Observable** — a future agent could check whether code conforms. "Functions use snake_case" beats "Write clean functions".
   - **Specific to this repo** — derived from what you read, not from generic best practices.
3. **Write** the draft to `.workbench/conventions.md`. Overwrite if the file is empty; otherwise refuse and tell the user to `wb conventions delete` first.
4. **Print** a one-line summary to stdout: e.g. `Wrote .workbench/conventions.md (5 sections, 18 bullets).`

## Required structure
Before creating the convention file, the agent should use any existing plans, agents.md, code used in the project, and readme to gain understanding of the conventions used consistently. If there are different languages in the codebase they should be treated independently in the conventions for things like testing frameworks and tools.

```markdown
# Project conventions

Conventions shared across all workbench plans in this repo. Plans may override by
adding their own `## Conventions` section — that section wins.


## Code style
- <bullets>

## Testing
- <bullets>

## Git
- <bullets>

## Naming
- <bullets>

## Other
- <bullets, optional — omit the section if you have nothing to add>
```

## What NOT to do

- Do not invent conventions you cannot trace back to something in the repo. If you don't know, leave the section bullet empty (`- (none observed)`).
- Do not include API keys, secrets, or any content from `.env*` files.
- Do not modify any file other than `.workbench/conventions.md`.
- Do not add prose paragraphs between bullets. Bullets only.
