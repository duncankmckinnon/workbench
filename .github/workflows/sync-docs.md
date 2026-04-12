---
description: Detect documentation-relevant changes in workbench and open a PR in wbcli-web to update the website docs.
on:
  push:
    branches: [main]
  workflow_dispatch:
  skip-if-match: 'is:pr is:open in:title "[docs-sync]" repo:duncankmckinnon/wbcli-web'
permissions:
  contents: read
  issues: read
  pull-requests: read
engine: gemini
steps:
  - name: Clone wbcli-web
    env:
      GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    run: |
      git clone https://x-access-token:${GH_TOKEN}@github.com/duncankmckinnon/wbcli-web.git /tmp/gh-aw/agent/wbcli-web
tools:
  github:
    toolsets: [default]
  bash:
    - "*"
  edit:
  web-fetch:
network:
  allowed:
    - defaults
    - node
safe-outputs:
  github-token: ${{ secrets.GH_AW_CROSS_REPO_PAT }}
  create-pull-request:
    max: 1
  noop:
    max: 1
---

# Sync Workbench Documentation to wbcli-web

You are a documentation sync agent. Your job is to detect when workbench documentation has changed and open a PR in the `duncankmckinnon/wbcli-web` Next.js site to bring the website docs up to date.

## Step 1: Identify what changed

Read the recent commits on this push to understand what changed. Focus on files that affect user-facing documentation:

- `README.md` — CLI reference, getting started, feature docs
- `workbench/cli.py` — CLI flags, commands, options
- `workbench/skills/use-workbench/SKILL.md` — skill reference and usage guide
- `workbench/skills/install-workbench/SKILL.md` — installation skill
- `workbench/skills/configure-workbench/SKILL.md` — configuration skill

Use the GitHub tools to read the commit diff. If none of the above files were modified, use the `noop` safe output with a message like "No documentation-relevant changes detected."

## Step 2: Understand the changes

For each changed file, understand the nature of the change:

- **New CLI flags or commands** — these need to appear in the CLI reference section
- **Changed flag behavior** — existing docs may need updating
- **New features or workflows** — may need new sections or examples
- **Removed features** — docs referencing them need cleanup
- **Skill updates** — the website skill reference should match

Summarize the documentation impact clearly before proceeding.

## Step 3: Read the current website docs

The `wbcli-web` repo has been pre-cloned to `/tmp/gh-aw/agent/wbcli-web`. Read files from there using bash (e.g. `cat`, `ls`, `find`).

Examine the docs-related pages in the Next.js site to understand:

- What is currently documented
- How the site structures its content (file layout, component patterns)
- Which specific files need updating based on the changes from Step 2

Look in `content/docs`:

- agents.mdx documents workbench agent configuration
- cli-reference.mdx is the straightforward CLI documentation for workbench
- getting-started.mdx walks through setup and examples
- plan-format.mdx documents how plans are formatted for parsing and examples
- profiles.mdx documents execution profiles and configuration for workflows
- running-plans.mdx gives example of how you can execute workbench runs with different flags and configurations in different scenarios
- skills.mdx gives an overview and explanation of the skills included in workbench
- tdd-mode.mdx is a brief explanation of the test-driven development option


## Step 4: Determine if a PR is needed

Compare the workbench changes (Step 2) against the current website content (Step 3). If the website already reflects the changes accurately, use the `noop` safe output: "Website docs already up to date."

A PR is needed when:
- New flags, commands, or features are missing from the website
- Existing documentation describes old behavior that has changed
- Examples or usage instructions are outdated
- Sections reference removed functionality

## Step 5: Create the PR

If updates are needed, create a pull request in `duncankmckinnon/wbcli-web` with:

- **Branch name:** `docs-sync/<short-description>` (e.g. `docs-sync/wave-control-flags`)
- **Title:** `[docs-sync] <concise description of what changed>`
- **Body:** Include:
  - Summary of what changed in workbench (with commit references)
  - What was updated in the website docs and why
  - Any sections that may need manual review or design attention

When editing the website files:
- Match the existing code style, component patterns, and formatting conventions
- Only change content that directly relates to the workbench changes
- Preserve the site's structure and layout — don't reorganize unrelated content
- If you're unsure about a site convention, note it in the PR description for human review

## Guidelines

- Be conservative: only propose changes you are confident are correct
- If a change is ambiguous (e.g. you can't find the right page to update), describe what needs updating in the PR body and mark it for human review rather than guessing
- Keep PR diffs minimal and focused — don't reformat or restructure unrelated content
- Reference the specific workbench commits that motivated each change
