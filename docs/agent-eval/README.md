# Coding-agent memory eval

This is the bar for Mnemo's primary path (spec §0): can Claude Code and Codex,
sharing one local Mnemo over MCP, carry what they learn from one session to the
next, across agents and across projects, without being told to use memory? It
replaces the chat-data extraction eval as the yardstick for this path. The
[scenarios](scenarios.json) were written before any run; the runner is
[`scripts/agent_eval.py`](../../scripts/agent_eval.py).

## What it measures

Ten scenarios, 23 sessions (11 Claude Code, 12 Codex). Each session is a separate
CLI invocation with no shared conversation, so only Mnemo can carry information.
Prompts sound like a developer talking and never mention Mnemo or tools.

| Scenario | Question |
| --- | --- |
| `recall_convention` | Does a convention told to Claude reach Codex later? |
| `handoff` | Does an end-of-day note from Codex reach Claude? |
| `correction` | Does "deploys moved from Tuesday to Thursday" replace Tuesday? |
| `multi_valued` | Is a third port added alongside two known ports? |
| `project_isolation` | Does a repo's fact stay out of another repo, and stay available in its own? |
| `global_preference` | Does "for all my projects: use uv" apply in another repo? |
| `no_duplicate` | Does a restated rule stay one memory? |
| `undo_wrong` | Does a retracted memory stop being current? |
| `no_junk` | Is small talk left unstored? |
| `no_invention` | With nothing stored, does the agent say it doesn't know? |

Checks are regular expressions on the final reply, counts of current memories
matching a pattern (optionally by scope), and the number of memory events a
session wrote. A scenario passes only if all its checks pass. The report also
counts sessions that called Mnemo at all, calls by tool, writes, Claude cost and
Codex tokens.

## Setup and isolation

One scratch database per run (created from `MNEMO_TEST_DSN`, dropped afterwards),
semantic embeddings (`ollama`, `nomic-embed-text`), a fresh namespace and fresh
fixture repositories (`github.com/mnemo-eval/alpha` and `beta`, containing only a
README and a stub) per scenario. Nothing in the user's configuration changes:

- **Claude Code:** `-p` with stream-JSON output, only the Mnemo MCP server
  (`--strict-mcp-config`), only project settings (the fixture's own), no session
  persistence, auto-memory off (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`), Mnemo tools
  pre-approved, default model, at most $0.60 per session and $6 per run.
- **Codex:** `exec --json --ephemeral`, read-only sandbox, hooks and plugins off,
  every other configured MCP server off, Mnemo tools approved, default model.

An arm adds files to every fixture repository with `--repo-file SRC[:DEST]` (for
example an `AGENTS.md`, or `.claude/settings.json` with hooks) and is labelled with
`--arm`. Each run writes `results.json` and the raw CLI output to a new directory;
existing evidence is never overwritten.

```bash
uv run python scripts/agent_eval.py --dry-run
uv run python scripts/agent_eval.py --arm baseline --out docs/agent-eval/runs/baseline
```

## Limits

One run per scenario: agents are not deterministic, so a single pass or failure is
an observation, not a rate. The checks and scenarios were written by the project's
agent, not by independent reviewers. The fixture repositories are tiny and the
facts are planted, so this measures carrying information between sessions, not
usefulness in real codebases. Claude and Codex use their configured default
models; the report records the Claude model, and Codex's is its CLI default.
