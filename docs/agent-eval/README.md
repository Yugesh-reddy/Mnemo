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

## Results

### Baseline (September 26, 2026): 4 of 10 scenarios, 11 of 20 checks

Today's setup with nothing added: the six direct tools, their MCP instructions, no
hooks and no instruction files ([results](runs/baseline/results.json), raw output in
`runs/baseline/raw`). All 23 sessions completed. Claude cost $0.75; Codex used 556k
input tokens (487k cached). `mnemo_dirty` records only an uncommitted `AGENTS.md`.

- **Codex never stored anything.** In all six sessions where Codex was told a fact
  (including "Please remember: …"), it replied as if it had saved it ("I'll
  remember that") without calling a tool. The handoff note, the correction and two
  of the three ports were lost.
- **Codex rarely checked memory before answering.** Three of its six answering
  sessions made no Mnemo call: it answered `pip install requests` although a
  global "use uv" memory existed, UNKNOWN in the repository that had the staging
  database name, and a PR title without the stored `WID-` convention.
- **Claude stored and recalled reliably.** It searched, then created, in all six
  sessions where it was told a fact, and searched before every answer. Every
  create failed once first because Claude passed a readable `request_id` instead
  of a UUID (six `INVALID_INPUT` errors, each retried).
- **Two passes are hollow.** `undo_wrong` and the store check of `no_duplicate`
  passed only because Codex never wrote the memory they test. Project isolation
  held (Codex in `beta` did not see `alpha`'s fact), but it was never tested against
  a Codex read in `alpha` that searched.

### Policy arm (September 26, 2026): 10 of 10 scenarios, 20 of 20 checks

Three changes from the baseline, all in commit `0cb5a23`: `request_id` is optional
(any text), the tool descriptions and server instructions say when to search and
save, and the [memory policy](../../mnemo/memory_policy.md) is in each fixture
repository's `AGENTS.md` and `CLAUDE.md` (`--repo-file`), standing in for the global
instruction files `mnemo-install` writes ([results](runs/policy/results.json)).
Claude cost $0.74; Codex used 719k input tokens (637k cached). The working tree
also held the user's `AGENTS.md` edit and the not-yet-committed installer, which the
server does not import.

Every pass was checked against the store and the transcripts, and none is hollow:

- **Codex now saves and corrects.** It stored the handoff note, both ports and the
  on-call day, each after searching, and changed Tuesday to Thursday with a guarded
  update (search, get, update). Claude withdrew the on-call memory the same way
  ("This team has no on-call handoff day").
- **Both agents search before answering.** 22 of 23 sessions used Mnemo; the one
  that did not was the small-talk session, which correctly stored nothing. Codex
  found the `WID-` convention, the staging database in `alpha` (and nothing in
  `beta`), and the global uv rule from another repository.
- **No duplicate, no friction.** Codex found the formatting rule already stored and
  did not add it again. There were no tool errors (baseline: six).

Which change did what: a Codex-only diagnostic isolates the policy file. Without it
Codex saved 0 of 6 facts in the [write diagnostic](runs/codex-write-diagnostic/)
(read-only or writable sandbox, reasoning effort none or medium) and 0 of 2 with the
new tool text alone ([tool-text diagnostic](runs/codex-tool-text-diagnostic/)); with
the policy in `AGENTS.md` it saved 2 of 2. With a writable sandbox and no policy,
Codex wrote the handoff to a `HANDOFF.md` file in the repository instead.

A first attempt at this arm stopped after four scenarios when the operator's session
ended; it is kept in [runs/policy-aborted-1](runs/policy-aborted-1/NOTE.md) and was
not scored. Its four completed scenarios also passed, with the same saves and searches.

## Limits

One run per scenario: agents are not deterministic, so a single pass or failure is
an observation, not a rate. The checks and scenarios were written by the project's
agent, not by independent reviewers. The fixture repositories are tiny and the
facts are planted, so this measures carrying information between sessions, not
usefulness in real codebases. Claude and Codex use their configured default
models; the report records the Claude model, and Codex's is its CLI default.
