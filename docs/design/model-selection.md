# Adapter model-selection reference

This table describes how each built-in adapter selects and passes the model to its CLI.

| Adapter | Binary | Headless flag | Model flag | Notes |
|---|---|---|---|---|
| `claude` | `claude` | `-p` | `--model` | Appended at end |
| `codex` | `codex` | `exec --full-auto --json` | `--model` | Inserted before positional prompt |
| `antigravity` | `agy` | `-p` | `--model` | Appended at end; safe to add after `--dangerously-skip-permissions` |
| `cursor` | `agent` | `-p` | `--model` | Appended at end |
| `copilot` | `copilot` | `-p` | `--model` | Appended at end |
