---
name: install-workbench
description: Use when installing workbench (wbcli), checking prerequisites (Python, git, tmux), or setting up the wb CLI in a local or global environment
---

# Installing Workbench

Step-by-step guide to install the `wb` CLI and verify all prerequisites.

## When to Use

- Installing workbench for the first time
- Checking whether prerequisites are met
- Troubleshooting installation or PATH issues
- Setting up workbench in a new environment (CI, container, new machine)

## Prerequisites Check

Before installing, verify each prerequisite. Run these checks and address any failures.

### 1. Detect OS

```bash
uname -s    # Darwin (macOS), Linux, or MINGW/MSYS (Windows)
```

### 2. Python 3.11+

```bash
python3 --version
```

**If missing or below 3.11:**

| Platform | Install |
|----------|---------|
| macOS | `brew install python` or download from [python.org](https://www.python.org/downloads/) |
| Linux (Debian/Ubuntu) | `sudo apt install python3` |
| Linux (Fedora/RHEL) | `sudo dnf install python3` |
| Windows | Download from [python.org](https://www.python.org/downloads/) or `winget install Python.Python.3.13` |

### 3. pip

```bash
python3 -m pip --version
```

**If missing:**

```bash
python3 -m ensurepip --upgrade
```

### 4. Git

```bash
git --version
```

**If missing:**

| Platform | Install |
|----------|---------|
| macOS | `xcode-select --install` or `brew install git` |
| Linux (Debian/Ubuntu) | `sudo apt install git` |
| Linux (Fedora/RHEL) | `sudo dnf install git` |
| Windows | Download from [git-scm.com](https://git-scm.com/downloads) or `winget install Git.Git` |

### 5. tmux (optional but recommended)

```bash
tmux -V
```

tmux enables live monitoring of agent sessions. Without it, use `--no-tmux` to run agents as raw subprocesses.

**If missing:**

| Platform | Install |
|----------|---------|
| macOS | `brew install tmux` |
| Linux (Debian/Ubuntu) | `sudo apt install tmux` |
| Linux (Fedora/RHEL) | `sudo dnf install tmux` |
| Windows | Available via WSL |

### 6. An agent CLI

At least one of:

| Agent | Check | Install |
|-------|-------|---------|
| Claude Code | `claude --version` | [docs.anthropic.com](https://docs.anthropic.com/en/docs/claude-code) |
| Gemini CLI | `gemini --version` | [github.com/google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli) |
| Codex | `codex --version` | [github.com/openai/codex](https://github.com/openai/codex) |
| Cursor CLI | `agent --version` | [cursor.com/docs/cli](https://cursor.com/docs/cli/overview) |

You can also use a custom agent CLI via `.workbench/agents.yaml` — see `wb agents add`.

## Install workbench

### Standard install

```bash
pip install wbcli
```

### Verify installation

```bash
wb --version
wb --help
```

### Install in a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate    # Linux/macOS
# .venv\Scripts\activate     # Windows
pip install wbcli
```

### Install from source (development)

```bash
git clone https://github.com/duncankmckinnon/workbench.git
cd workbench
pip install -e ".[dev]"
```

## Setup a repo

After installing, set up workbench in your project:

```bash
cd your-project
wb setup
```

This creates `.workbench/`, installs bundled skill files for your agent platform, and prepares the repo for `wb run`.

### Setup options

```bash
wb setup                           # auto-detect agent, install skills locally
wb setup --agent claude            # install skills for Claude Code
wb setup --agent gemini            # install skills for Gemini CLI
wb setup --agent cursor            # install skills for Cursor CLI
wb setup --profile                 # also create a profile.yaml
wb setup --global                  # install skills to user-level paths only
wb setup --symlink                 # symlink instead of copy (stays in sync)
wb setup --update                  # force-update skills to latest version
```

### Initialize agent config (optional)

Generate `.workbench/agents.yaml` with all built-in adapter configs:

```bash
wb agents init
```

This is optional — built-in adapters work without a config file. Use this if you want to customize agent invocation flags or add custom agents.

### Initialize a profile (optional)

```bash
wb profile init                                        # defaults
wb profile init --set reviewer.agent=gemini            # customize
wb profile init --name fast --set implementor.agent=codex
```

## Verify everything works

Run a quick end-to-end check:

```bash
# 1. Confirm wb is on PATH
wb --version

# 2. Confirm git repo
git rev-parse --show-toplevel

# 3. Confirm .workbench/ exists
ls .workbench/

# 4. Preview a plan (dry run, no agents needed)
wb preview your-plan.md

# 5. Run a plan (requires an agent CLI)
wb run your-plan.md --no-tmux
```

## Troubleshooting

### `wb: command not found`

pip installed to a directory not on PATH. Common fixes:

```bash
# Check where pip installed it
python3 -m pip show wbcli | grep Location

# Add to PATH (add to ~/.bashrc or ~/.zshrc for persistence)
export PATH="$HOME/.local/bin:$PATH"

# Or use pipx for isolated installs
pipx install wbcli
```

### `tmux is required but not found`

Either install tmux (see above) or run with `--no-tmux`:

```bash
wb run plan.md --no-tmux
```

### `Not in a git repository`

workbench requires a git repo. Initialize one:

```bash
git init
git add .
git commit -m "initial commit"
```

### Permission errors on install

```bash
pip install --user wbcli    # install to user site-packages
# or
sudo pip install wbcli      # system-wide (not recommended)
```

## Updating

```bash
pip install --upgrade wbcli    # upgrade the package
wb setup --update              # update project-level skills
wb setup --global --update     # update user-level skills
```
