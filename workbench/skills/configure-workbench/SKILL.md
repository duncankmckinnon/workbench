---
name: configure-workbench
description: Use when setting up workbench in a repo, configuring agent adapters (.workbench/agents.yaml), managing profiles (profile.yaml), or troubleshooting agent dispatch
---

# Configuring Workbench

How to set up the `wb` CLI, configure agent adapters, and manage profiles for multi-agent orchestration.

## When to Use

- Setting up workbench in a new repo (`wb setup`)
- Configuring which AI agents to use for each pipeline role
- Creating or editing `.workbench/agents.yaml` for custom agent adapters
- Creating or editing `.workbench/profile.yaml` to control agent behavior per role
- Troubleshooting agent dispatch, connection, or output parsing issues
- Switching between agent platforms (Claude, Gemini, Codex, Cursor, Copilot)

## Initial Setup

```bash
wb setup                           # auto-detect agent, install skills, create .workbench/
wb setup --agent claude            # install skills for Claude Code
wb setup --agent gemini            # install skills for Gemini CLI
wb setup --profile                 # also create a profile.yaml
wb setup --global                  # install skills to user-level paths only
```

`wb setup` creates the `.workbench/` directory, installs the bundled skill files for your agent platform, and optionally creates a starter profile.

## Agent Adapters

Workbench dispatches work to AI coding agents via adapters. Each adapter knows how to invoke a CLI, pass the prompt, and parse the output.

### Built-in Adapters

| Name | CLI Command | Mode | Output |
|------|-------------|------|--------|
| `claude` | `claude -p <prompt>` | Print mode, JSON output | Parses `result` and `cost_usd` from JSON |
| `gemini` | `gemini -p <prompt>` | Print mode, JSON output, yolo approval | Parses `response` and `stats` from JSON |
| `codex` | `codex exec <prompt>` | Full-auto, JSON events | Extracts last assistant message from NDJSON |
| `cursor` | `agent -p <prompt>` | Print mode, text output | Raw text |
| `copilot` | `copilot -p <prompt>` | Print mode, JSON output, no-ask-user | Extracts last assistant message from JSONL |

Use `--agent <name>` on `wb run` or `wb merge` to select one:

```bash
wb run plan.md --agent gemini
wb run plan.md --agent cursor
```

### Initialize Agent Config

Generate `.workbench/agents.yaml` with all built-in adapter configs as a starting point:

```bash
wb agents init
```

This creates a YAML file you can customize. The file controls how each agent is invoked.

### agents.yaml Format

```yaml
agents:
  claude:
    command: claude
    args: ["-p", "{prompt}", "--output-format", "json", "--allowedTools", "Edit,Write,Read,Glob,Grep,Bash(git *),Bash(uv run *),Bash(cd *),Bash(ls *),Bash(npx *)"]
    output_format: json
    json_result_key: result
    json_cost_key: cost_usd
  my-custom-agent:
    command: my-cli
    args: ["--headless", "{prompt}"]
    output_format: text
```

**Fields:**

| Field | Description | Default |
|-------|-------------|---------|
| `command` | CLI executable to run | *(required)* |
| `args` | Argument list. `{prompt}` is replaced with the agent prompt at runtime | `["{prompt}"]` |
| `output_format` | `text` or `json` | `text` |
| `json_result_key` | JSON key containing the agent's response | `result` |
| `json_cost_key` | JSON key containing cost/usage data | `cost_usd` |

### Managing Agents via CLI

```bash
wb agents init                    # create agents.yaml with built-in defaults
wb agents list                    # show built-in and custom agents
wb agents show claude             # show full config for an agent
wb agents add my-agent --command my-cli --args "--headless,{prompt}" --output-format json
wb agents add my-agent --command new-cli   # update an existing agent
wb agents remove my-agent         # remove a custom agent
```

### Resolution Order

When `wb run --agent <name>` is used:
1. If `.workbench/agents.yaml` contains the name, use that config
2. If it's a built-in name (`claude`, `gemini`, `codex`, `cursor`, `copilot`), use the built-in adapter
3. Otherwise, fall back to a generic adapter that passes the prompt as the sole argument

This means you can override a built-in adapter's behavior by adding an entry with the same name to `agents.yaml`.

## Profiles

Profiles control which agent and instructions are used for each pipeline role (implementor, tester, reviewer, fixer, merger).

### Create a Profile

```bash
wb profile init                                        # create .workbench/profile.yaml
wb profile init --global                               # create ~/.workbench/profile.yaml
wb profile init --set reviewer.agent=gemini            # with inline overrides
wb profile init --name fast --set reviewer.agent=gemini  # named profile
```

### profile.yaml Format

```yaml
roles:
  reviewer:
    agent: gemini
    directive: "Focus on security and correctness."
  tester:
    directive_extend: "Also check edge cases for null inputs."
  implementor:
    agent: codex
```

Only include roles and fields you want to override. Everything else uses built-in defaults.

### Role Fields

| Field | Description |
|-------|-------------|
| `agent` | CLI command for this role (default: `claude`) |
| `directive` | Full replacement for the role's default instructions |
| `directive_extend` | Text appended to default instructions (cannot combine with `directive`) |

### Named Profiles

Store multiple configurations as `profile.<name>.yaml`:

```bash
wb profile init --name security --set reviewer.directive="Focus only on security vulnerabilities."
wb run plan.md --profile-name security
```

### Profile Merge Order

Profiles merge in order (later overrides earlier):
1. Built-in defaults
2. `~/.workbench/profile.yaml` (user-level)
3. `.workbench/profile.yaml` (project-level)
4. `--profile <path>` flag
5. CLI directive flags (`--reviewer-directive`, etc.)

### Profile Commands

```bash
wb profile show                    # print resolved profile
wb profile show --name fast        # show a named profile
wb profile set reviewer.agent gemini  # update a field
wb profile diff                    # show differences from defaults
```

## Multi-Agent Configurations

### Different Agents Per Role

Use profiles to assign different agents to different roles:

```yaml
roles:
  implementor:
    agent: claude
  tester:
    agent: claude
  reviewer:
    agent: gemini
  fixer:
    agent: claude
```

Or via CLI:

```bash
wb profile init --set implementor.agent=claude --set reviewer.agent=gemini
```

### Custom Instructions Per Role

Tailor agent behavior without changing the plan:

```yaml
roles:
  tester:
    directive_extend: "Run pytest with -x flag. Focus on edge cases."
  reviewer:
    directive: "Review only for security vulnerabilities and data validation. Ignore style."
```

Or one-off via CLI flags:

```bash
wb run plan.md --reviewer-directive "Focus only on security issues."
wb run plan.md --tester-directive "Run pytest with -x flag, fail fast."
```

## Troubleshooting

### Agent not found

```
Error: Agent error: [Errno 2] No such file or directory: 'my-agent'
```

The command in `agents.yaml` or the `--agent` flag doesn't match an executable on PATH. Verify with `which <command>`.

### Wrong output parsing

If agent output shows raw JSON or garbled text, check:
- `output_format` matches what the CLI actually produces
- `json_result_key` matches the key in the CLI's JSON output
- For custom agents, test the command manually: `my-cli --headless "hello"` and inspect output

### Agent crashes in pipeline

Tasks that crash (agent timeout, connection error) show as `failed` with no fix attempts. Use `--retry-failed` to automatically re-run these:

```bash
wb run plan.md --retry-failed
```

For persistent failures, re-run specific tasks with adjusted settings:

```bash
wb run plan.md -b workbench-1 --task task-2 --implementor-directive "Try a simpler approach."
```

### Viewing agent sessions

With tmux (default), attach to watch any agent work in real time:

```bash
tmux attach -t wb-task-1-implementor
```

Sessions are named `wb-task-<N>-<role>`. Use `wb stop` to kill all sessions.
