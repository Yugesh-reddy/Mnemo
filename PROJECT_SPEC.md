# Mnemo — Project Spec (single source of truth, v4)

The September 9, 2026 correctness amendment in §14 supersedes earlier wording
where indicated. The implementation plan is in `docs/CORRECTNESS_PLAN.md`;
executed checks and remaining quality limits are in `PROJECT_STATUS.md`.
The September 11 quality work follows `docs/QUALITY_RELIABILITY_PLAN.md`;
`docs/quality-v2/README.md` records source review and frozen validation results.

> **Working name:** Mnemo · **One-liner:** *Agent memory that stores less and remembers what matters.*
> A quality gate keeps junk out, provenance tells you where every fact came from, and one-click
> rollback fixes what slips through — on an append-only, versioned, bitemporal store.
>
> **This file is the build map for Claude Code.** Put it + `CLAUDE.md` at the repo root.
> The *design rationale + citations* live in `MODEL_COUNCIL_solution.md`; this is the build spec.
> Current progress is tracked separately in `PROJECT_STATUS.md` (spec = target, status = progress).

---

## 0. One product, not two (read this first)
Earlier drafts split into "git for agent memory" (versioning) and a "quality pipeline." **They are the same product.** Resolution, final:
- **The product is the write-quality pipeline** — deciding *what is worth remembering* (salience scoring, NLI verification, tiering, dedup, decay). This attacks the loud, validated pain: agent memory stores ~96–98% junk and invents false facts (Mem0 issue #4573).
- **The versioned event store is its foundation** — every memory is an append-only event, so the quality decisions (keep / demote / drop / forget) are all **auditable and reversible**. Incumbents with mutable stores literally cannot make that guarantee.
- **`blame` / `revert` are the safety net** — for whatever junk or wrong fact slips through the gate, a human (or agent) can see where it came from and undo it.
- **Lead with the outcome (debugging / stores-less), keep the architecture honest (it's a versioned memory store).** Hero demo = the precision/recall side-by-side; rollback = the secondary demo.

---

## 1. Tech stack (pinned)
- Python 3.12, full type hints. ruff + black. Pydantic v2 (+ `pydantic-settings`, `.env`).
- **Postgres 16 + pgvector (≥0.7, HNSW).** Access via **asyncpg**; core SQL (events, as-of, diff, HEAD) stays as readable `.sql` migrations — the legible data model is part of the value.
- **Embeddings/LLM via abstractions** with two backends each: OpenAI (`text-embedding-3-small`, `gpt-4o-mini`) and local Ollama (`nomic-embed-text` 768-d, a small instruct model). Default OpenAI; local path must work.
- **MCP-first** integration (official `mcp` SDK, stdio) + a thin Python SDK. The OpenAI-compatible proxy is **optional Phase-1.5**, not MVP.
- Web UI: FastAPI + HTMX + Tailwind (server-rendered, minimal). CLI: Typer.
- Tests: pytest + pytest-asyncio against a disposable Postgres (docker-compose). `docker compose up` + one command runs the demo.
- **Embedding dimension** is centralized in config + migrations. Default `vector(1536)`; for Ollama use 768. Never mix dims.

---

## 2. Architecture in one picture
```
turn ─▶ observe() ─▶ [fast cache | enqueue]
                                  │  (async worker)
   WRITE PATH (the quality pipeline, council Layers 0–5):
   0 extract (salience-first decomposition, importance 1–10, tier guess)
   1 pre-filter (free regex: negation / hypothetical / transient)
   2 verify   (NLI entailment vs source; accept only "entailment")
   3 dedup    (canonical fact_key + embedding ≈0.90 → UPDATE, else new)
   4 score+tier (write_score; DEMOTE borderline, drop only ephemeral)
   5 commit   (append-only memory_event w/ importance, score, tier, provenance, source_span)
                                  │
   BACKGROUND: consolidation/reflection (gated, trust-safe) · Ebbinghaus decay/reinforce
                                  │
   STORE: memory_fact (identity+HEAD) · memory_event (append-only log) · memory_current (view)
                                  │
   READ: search = HNSW top-k → rerank by (recency, relevance, importance), tier+validity filtered
   SAFETY NET / VERSIONING: add · blame · revert · diff · log · commit
```

---

## 3. Database schema (design baseline)
The executable schema is the numbered migrations (`migrations/0001_init.sql` …).
The design sketch below predates the §14 queue, decision and temporal additions.
Its `agent_reflection` enum member describes a future consolidation requirement;
the current `mem_provenance` enum intentionally has no reflection writer/member.
The configured fresh-install dimension defaults to 768, not the sketch's 1536.
Includes council fields, versioning spine, and structured `source_span` (NOT `source_text`).

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE mem_kind AS ENUM ('text_fact','triple','kv','task','trace');
CREATE TYPE mem_op   AS ENUM ('ADD','UPDATE','INVALIDATE','REVERT','DELETE');
CREATE TYPE mem_tier AS ENUM ('durable','session','ephemeral');
CREATE TYPE mem_prov AS ENUM ('direct_user_statement','agent_inference','tool_output',
                              'document','human_review','agent_reflection');
CREATE TYPE mem_trust AS ENUM ('high','medium','low');

CREATE TABLE memory_fact(
  fact_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace text NOT NULL DEFAULT 'default', user_id text NOT NULL DEFAULT 'default',
  agent_id text NOT NULL DEFAULT 'default', session_id text,
  subject text NOT NULL, predicate text NOT NULL, fact_key text NOT NULL,
  kind mem_kind NOT NULL DEFAULT 'triple', status text NOT NULL DEFAULT 'active',
  current_event_id uuid, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(namespace,user_id,agent_id,fact_key));

CREATE TABLE memory_event(
  seq bigint GENERATED ALWAYS AS IDENTITY,
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  fact_id uuid NOT NULL REFERENCES memory_fact(fact_id),
  op mem_op NOT NULL,
  object_text text, object_number numeric, object_json jsonb,
  embedding vector(1536),
  -- quality-gate fields
  importance int CHECK (importance BETWEEN 1 AND 10),
  write_score real, tier mem_tier NOT NULL DEFAULT 'durable',
  provenance mem_prov NOT NULL, actor text, trust_level mem_trust NOT NULL DEFAULT 'medium',
  reason text, source_span jsonb,          -- {"turn_ids":[...], "source_event_ids":[...], "doc":...}
  -- bitemporal + versioning spine
  valid_from timestamptz DEFAULT now(), valid_to timestamptz,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  superseded_at timestamptz, superseded_by uuid REFERENCES memory_event(event_id),
  parent_event_id uuid REFERENCES memory_event(event_id),
  -- decay / reinforcement
  strength real DEFAULT 1.0, recall_count int DEFAULT 0, last_used timestamptz DEFAULT now(),
  commit_id uuid);

ALTER TABLE memory_fact ADD CONSTRAINT fk_head
  FOREIGN KEY (current_event_id) REFERENCES memory_event(event_id);

CREATE TABLE memory_commit(
  commit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace text NOT NULL DEFAULT 'default', parent_commit_id uuid REFERENCES memory_commit(commit_id),
  label text, at_seq bigint NOT NULL, created_by text, created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE memory_event ADD CONSTRAINT fk_commit
  FOREIGN KEY (commit_id) REFERENCES memory_commit(commit_id);

CREATE TABLE fast_cache(                     -- synchronous working-memory tier
  cache_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace text NOT NULL DEFAULT 'default', user_id text NOT NULL DEFAULT 'default',
  session_id text NOT NULL, turn_id text NOT NULL, raw_text text NOT NULL,
  embedding vector(1536), reconciled boolean NOT NULL DEFAULT false,
  reconciled_event_id uuid REFERENCES memory_event(event_id), created_at timestamptz DEFAULT now());

CREATE TABLE extraction_job(                  -- async worker queue
  job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace text DEFAULT 'default', user_id text DEFAULT 'default', agent_id text DEFAULT 'default',
  session_id text, turn_id text NOT NULL, payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending', attempts int NOT NULL DEFAULT 0,
  locked_at timestamptz, created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now());

CREATE INDEX idx_fact_key   ON memory_fact(namespace,user_id,agent_id,fact_key);
CREATE INDEX idx_event_fact ON memory_event(fact_id, recorded_at DESC);
CREATE INDEX idx_event_seq  ON memory_event(seq);
CREATE INDEX idx_event_head ON memory_event(fact_id) WHERE superseded_at IS NULL;
CREATE INDEX idx_event_embed ON memory_event USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_cache_sess ON fast_cache(session_id, reconciled);
CREATE INDEX idx_job_status ON extraction_job(status, created_at);

CREATE VIEW memory_current AS
  SELECT f.fact_id,f.subject,f.predicate,f.kind,e.*
  FROM memory_fact f JOIN memory_event e ON e.event_id=f.current_event_id
  WHERE f.status='active' AND e.tier IN ('durable','session');   -- ephemeral hidden from HEAD
```
Plus `fact_state_as_of(namespace,user,seq)` (latest non-superseded event per fact ≤ seq, op ∈ ADD/UPDATE/REVERT) for `diff` / time-travel.

---

## 4. The write path — quality pipeline semantics (Layers 0–5)
Runs async behind the fast cache. The *why* (formulas, citations) is in `MODEL_COUNCIL_solution.md`.

0. **Extract** — salience-first fact decomposition (FActScore/FactEHR style): atomic facts only, **exclude** pleasantries/acknowledgments; emit `{subject,predicate,object,kind,importance∈[1,10],tier_guess,assertion_type}`; controlled predicate vocab; **never** extract user-facts from assistant turns.
1. **Pre-filter (free)** — regex drops obvious negation / hypothetical / pure-transient before any model call.
2. **Verify** — NLI entailment of candidate vs source turn; accept only `entailment`. Cheap NLI gatekeeper auto-accepts at p>0.99, delegates the ambiguous minority to a cheap LLM. (This is what defeats "I don't use X", "what if I switched", sarcasm, assistant-fabrication.)
3. **Dedup / resolve** — canonical `fact_key` + embedding ≈0.90 → **UPDATE** the existing `fact_id` (an event), else new fact. LLM-as-judge merge only for the ambiguous middle band.
4. **Score + tier** — `write_score = w_imp·(importance/10) + w_spec·specificity + w_nov·novelty − w_src·assistant_penalty`. **Demote borderline (→ session / short TTL), drop only ephemeral noise.** Never hard-drop a borderline fact.
5. **Commit** — append-only `memory_event` carrying `importance, write_score, tier, provenance, trust_level, reason, source_span`.

**Background jobs**
- **Consolidation/reflection (Layer 4, build LAST)** — cluster episodics → synthesize semantic facts. **Trust-laundering safeguards (mandatory):** tag `provenance='agent_reflection'`, default **medium** trust (never above sources), keep **all** source episodic `event_id`s in `source_span` so `blame` traces through, and run the **NLI gate against all source episodics**.
- **Decay/reinforce (Layer 5)** — Ebbinghaus `R=e^(−t/S)`, importance-modulated (`λ_eff=0.16·(1−imp·0.8)`); on recall `S+=1, t→0`; below threshold → **archive (reversible), never delete**.

---

## 5. Versioning ops (the foundation + safety net)
Each is one transaction over the append-only log; `memory_fact.current_event_id` is the HEAD pointer, maintained by `add`/`revert`.
- **`add`** — canonical key → dedup/UPDATE-vs-new (see Layer 3) → append event → move HEAD.
- **`search`** — HNSW top-k over `memory_current`, then **rerank** by `α_rec·recency + α_rel·relevance + α_imp·(importance/10)`, tier+validity filtered, merged with un-reconciled fast-cache for the session. (Rerank over top-k — **not** a custom index.)
- **`blame`** — full ordered event history for a fact (op, object, provenance, actor, importance, reason, source_span, recorded_at). Traces through consolidation via `source_span`.
- **`revert`** — append a `REVERT` event restoring a prior payload; supersede current; move HEAD; **never mutate history**.
- **`diff(a,b)` / `log` / `commit`** — as-of state join (+/~/-); event timeline; named pointer at `max(seq)`.

---

## 6. Two-tier memory + handshake
`observe()` writes the turn to `fast_cache` **synchronously** (immediate next-turn recall) and enqueues extraction. `search` merges un-reconciled cache rows + semantic HEAD, **deduped by fact_key/turn_id**. **Handshake:** when the worker commits a reconciled fact, set `fast_cache.reconciled=true` for that `turn_id`; the merge drops reconciled raw rows in favor of the semantic fact (no stale reads, no double-count).

---

## 7. The eval harness — the north star (first-class, NOT a non-goal)
`mnemo/eval.py` + `make eval`. Given a labeled conversation, ground-truth (predicate,value) sets, and a forbidden set, report **precision / recall / F1 / false-fact count** for naive (store-everything) vs gated. Build it **early** and tune every gate knob against it. Run it on a labeled **LoCoMo / LongMemEval** conversation for the real number — that number is the demo. (We measure precision/recall as a quality gate; we do **not** position the project as a retrieval-leaderboard winner.)

---

## 8. MCP server + SDK
SDK (`mnemo.Mnemo`): `add, search, blame, revert, diff, log, commit, observe`. MCP tools (stdio): `memory_observe, memory_search, memory_add, memory_get, memory_blame, memory_revert, memory_diff, memory_log`. Tight, action-oriented tool descriptions.

---

## 9. Demos & definition of done
- **Hero demo (the pitch):** precision/recall side-by-side — same conversation, naive vs Mnemo (`make eval`), e.g. junk and false memories removed, important facts kept. This is the GIF.
- **Secondary demo:** rollback — agent states a wrong fact → web UI → `blame` shows the bad source → revert → behavior corrects.

**Done (MVP):** `docker compose up` + a single command runs both demos; these tests pass — fact-lifecycle (ADD→UPDATE→REVERT, history immutable), two-tier handshake (no stale read / no double-count), **gate prevents false memories + dedups + demotes**, and the precision/recall eval; MCP server callable externally; web UI does search→blame→revert.

---

## 10. Web UI (minimal)
FastAPI + HTMX + Tailwind. (1) Memory list/search (subject, predicate, value, **tier, importance, trust**, updated). (2) Fact detail = blame view with per-event Revert + inline Edit. (3) Diff view (two commits → +/~/-). Demo surface, not a product.

---

## 11. Milestone roadmap (canonical order)
Foundation is largely built already (real Postgres, embeddings, LLM extractor, MCP, UI). Forward order:
1. **Foundation** — event store + version ops (add/search/blame/revert/diff/log/commit). ✅ shared core.
2. **Eval harness FIRST** — precision/recall vs naive baseline on a labeled ~200-turn convo (the north star).
3. **Extraction + Layers 0–3** — salience decomposition, regex pre-filter, **NLI verify**, dedup. Biggest precision impact.
4. **Decay + tiering (Layer 5)** — Ebbinghaus decay + reinforcement + reversible archive. (Before consolidation.)
5. **Consolidation/reflection (Layer 4) LAST** — only once the eval shows episodic bloat costs precision, and only with the §4 trust-laundering safeguards.
6. **Polish** — README to the (real) quality story; record the eval GIF.

**Reliability fixes to fold in where they touch the same code:** R1 orphaned-job recovery (worker), R2 MCP connection pool, R3 concurrent-`add` race (`add()`/`_insert_fact`). Each = a focused diff + test + commit.

---

## 12. Non-goals (corrected — these stay out of the MVP)
- ❌ OpenAI-compatible **proxy** (Phase 1.5; MCP + SDK only now).
- ❌ Full git-style **branching/merge**; **PR-for-beliefs** beyond a non-blocking stub (Phase 2).
- ❌ **Multi-tenant** auth/isolation (namespace/scope columns exist; no auth).
- ❌ **Graph/network** memory views (future *view* over the event log).
- ❌ "**Memory CI/CD**", hosted/billing mode.
- ❌ Positioning the project as a **retrieval-accuracy leaderboard winner**. (The eval harness itself **is in scope** — it's our quality measure, not a marketing claim.)

> Correction from v1: the precision/recall **eval is no longer a non-goal** — it's the north star (§7). Only *leaderboard-as-positioning* stays out.

---

## 13. Parameter starting points (tune against the eval — §7)
| Knob | Start | Source |
|---|---|---|
| importance | 1–10, LLM-assigned | Generative Agents (Park 2023) |
| NLI auto-accept | entailment p > 0.99 | Deep-Research gatekeeper (2026) |
| dedup → UPDATE | cosine ≥ 0.90 | common practice |
| write-score weights | w_imp .4 / w_spec .3 / w_nov .3 | tune for F1 |
| tier: durable | write_score ≥ 0.70 (demote below) | — |
| decay | R=e^(−t/S), S+1 on recall | MemoryBank (Zhong 2024) |
| importance-mod decay | λ_eff = 0.16·(1−imp·0.8) | production variant (2026) |
| consolidation trigger | every 50 events / cluster ≥ 3 | Generative Agents |
| retrieval recency γ | 0.995 / hour | Generative Agents |

Build the smallest thing that makes the two demos real; tune everything against the eval; ship consolidation last.

## 14. Correctness amendment — September 9, 2026

These contracts implement the requested correctness milestone. They replace the
incomplete identity, verification, temporal and evaluation rules in §§3–7; the
original thresholds remain unvalidated priors.

- **Transactions and identity.** Mutations lock namespace/user/agent scope and the
  fact row before reading HEAD. A revert target must belong to that scoped fact;
  SQL copies its typed payload, embedding and source lineage into a new event.
  Archival owns its transaction and rechecks HEAD and last recall. Exact visible
  values and narrow aliases deduplicate; embedding similarity cannot suppress a
  changed number/date/value or merge different structured identities.
- **Worker ownership.** `make mcp` starts extraction and scheduled decay by
  default; `mnemo-worker` supports a separate consumer. Jobs define their own
  scope, exact cache row, retry availability, attempts, token and renewable lease.
  Model calls run outside transactions. Ownership is checked under a row lock
  before atomic event/decision/cache/completion writes. Shutdown stops claims,
  gives in-flight work ten seconds, then cancels and requeues owned work; bounded
  synchronous HTTP requests may finish after cancellation.
- **Verification and provenance.** The verifier receives a typed
  `{subject,predicate,object}` assertion and source evidence. Assistant turns are
  excluded before model calls. Extractor assertions always become
  `agent_inference` with low trust; the model cannot choose a stronger provenance.
  Optional NLI reads model label metadata and delegates ambiguous cases to a
  bounded JSON fallback. Hypotheses must preserve the relation, including
  preference versus use. Heuristic mode is explicitly a regression backend;
  clause-local rules do not establish general semantic entailment. Broad regex
  rejection must not remove a factual clause beside an unrelated hypothetical.
  Explicit SDK/MCP `add()` is the caller-controlled direct-write API.
- **Decision history.** `quality_decision` is append-only and retains candidates,
  rejected/demoted/accepted/duplicate/error outcomes, evidence, verdict, scoring
  components, configuration fingerprints and source IDs. It is separate from
  retrieval-visible beliefs. Queue health and decision history are exposed by
  commands, MCP tools, JSON endpoints and the operations UI.
- **Temporal contract.** Event `session_id` and `expires_at` describe session and
  retention independently of world validity. Session facts require a session and
  default to a 24-hour TTL. Legacy events receive the same read-time expiry from
  recorded_at without rewriting payloads. Current reads select active HEAD and
  filter `[valid_from, valid_to)` plus expiry. A future HEAD hides older revisions;
  there is no implicit fallback to an earlier event. Historical search selects
  the latest revision recorded by `as_of=T`, then filters world validity at
  `valid_at=V` (default T) and retention at T. Require aware datetimes. History
  excludes cache and reinforcement. Session search is restricted to that session;
  administrative get/list/history remain scoped to namespace/user/agent.
- **Retrieval.** Bound vector and lexical candidates independently before
  reranking. The live vector query orders raw distance with a limit on the indexed
  event table and checks scoped HEAD membership; verify the actual plan. Historical
  candidate selection is exact. Approximate filtered HNSW can underfill k. Search
  uses pgvector >= 0.8 iterative scans with a configured work bound to continue
  past superseded or out-of-scope neighbors. Search
  owns a repeatable-read transaction for both tiers; when called inside a caller
  transaction it inherits that transaction's isolation. Merge and deduplicate
  both tiers before the final ranking limit.
- **Evaluation.** Preserve the scripted 18-turn regression, add a versioned
  200-turn synthetic development/held-out corpus, and adapt external conversations
  only with explicit atomic labels. Compare complete normalized assertions and
  judge every write against its own source turn, including superseded writes.
  Keep explicit false writes separate from unmatched labels. Report final/history
  precision and recall, must-keep coverage, total rows/events/allocated bytes,
  latency, measured usage and cost when supplied prices permit it. Retain assertion
  snapshots before cleanup. Synthetic scores and provisional labels establish
  neither real-world accuracy nor competitor results.
- **Installation and CI.** Package migrations, templates, evaluation data and demo
  modules. Declare UI runtime dependencies and optional NLI; use the supported MCP
  1.x API. A fresh database renders the configured vector dimension; existing
  dimensions must match and require explicit re-embedding when changed. Required
  Postgres CI fails when its database is unavailable. Validate a built wheel from
  a clean environment outside the source checkout.

Consolidation stays disabled until evaluation demonstrates a benefit. Adding the
reflection enum alone would not complete it: it still needs every source event,
verification against source evidence, and trust no higher than medium or its
least-trusted source.
