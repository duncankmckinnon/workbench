# Contributing to Workbench

Thanks for your interest in contributing!

## Getting started

```bash
git clone https://github.com/duncankmckinnon/workbench.git
cd workbench
pip install -e ".[dev]"
pre-commit install
```

## Development workflow

1. Create a branch from `main`
2. Make your changes
3. Run tests: `uv run pytest`
4. Ensure formatting passes: `black . && isort .`
5. Open a pull request against `main`

All PRs require CODEOWNERS approval and passing CI before merge.

## Code style

- **Formatter:** Black (line length 99)
- **Import order:** isort with `profile = "black"`
- **Pre-commit hooks** run automatically on commit — install with `pre-commit install`
- Python 3.11+, type hints on all function signatures
- `from __future__ import annotations` in every module
- Imports: stdlib, then third-party, then local (separated by blank lines)

## Testing

- Framework: pytest + pytest-asyncio
- Run: `uv run pytest` or `pytest tests/ -v`
- Tests go in `tests/` and mirror the module structure
- Use fixtures from `tests/conftest.py` where possible

## Project structure

```
workbench/
├── agents.py          # Agent spawning and pipeline
├── cli.py             # Click CLI (wb command)
├── orchestrator.py    # Plan execution and wave orchestration
├── plan_parser.py     # Markdown plan parsing
├── worktree.py        # Git worktree management
├── tmux.py            # Tmux session management
├── adapters.py        # Agent CLI adapters
└── skills/            # Bundled agent skill files
```

## Reporting issues

Open an issue at https://github.com/duncankmckinnon/workbench/issues with:

- What you expected vs. what happened
- Steps to reproduce
- Python version and OS
