# Mnemo — build status (spec v3 = target, this file = progress)

Updated 2026-07-05. All milestones below are committed on `main`, one commit per task,
tests green (88) + ruff/black clean at every step.

## Done (runnable + tested, on real Postgres 16 + pgvector)
- [x] Event-sourced store, append-only, HEAD pointer, bitemporal columns (M0–M2)
- [x] Version ops: add / search / blame / revert / diff / log / commit / **invalidate**
- [x] Real embeddings (Ollama `nomic-embed-text` 768-d default, OpenAI switch) +
      similarity routing (update_sim 0.90 / dedup_sim 0.92) (M3)
- [x] Two-tier fast-cache + extraction worker + invalidation handshake (M4)
      · R1 orphaned-job lease recovery · R3 concurrent-add race · observe() hash dedup
- [x] MCP server (8 tools, stdio, pooled, compact search results) + Python SDK (M5, R2)
- [x] Reference agent + `make demo` rollback scenario (M6) · web UI with tier/importance
      badges (M7)
- [x] **Eval harness FIRST** (`make eval`): labeled 18-turn conversation, naive-vs-gated
      precision/recall/F1/false-count (spec §7)
- [x] **Quality gate (Layers 1–2)**: HeuristicVerifier (negation scope-aware,
      hypotheticals), write_score (.4/.3/.3 + assistant/transient penalties),
      tier durable ≥ .70 / session ≥ .45 / drop below — demote-don't-drop
- [x] **Gated write path in the worker** — events carry importance/write_score/tier/reason
- [x] **Decay + reinforcement (Layer 5)**: R = e^(−λ_eff·t/S), λ_eff = 0.16·(1−imp·0.8);
      recall → S+1; decay_sweep archives via appended event (reversible)
- [x] **Hybrid retrieval**: english FTS + cosine + composite rerank
      (0.5·rel + 0.2·recency + 0.3·imp), auto-reinforce on recall
- [x] Bitemporal enforcement: `memory_current` filters valid_to; `invalidate()` op

## The number (make eval, deterministic FakeEmbedder)
```
NAIVE : stored 15 | P  60.0% | R 100.0% | F1  75.0% | false 2
GATED : stored 10 | P  90.0% | R 100.0% | F1  94.7% | false 0
```
All four CLAUDE.md must-pass tests are green: fact-lifecycle, two-tier handshake,
quality gate (no false memories, dedup, demote-not-drop), eval (gated > naive,
false = 0, recall 100%).

## Open
- [ ] Consolidation/reflection (Layer 4) — deliberately LAST (spec §11.5), only with the
      trust-laundering safeguards (`agent_reflection` provenance, medium trust,
      source_span event_ids, NLI gate vs all sources)
- [ ] Real NLI entailment backend behind the Verifier seam (heuristic regex today)
- [ ] Run the eval on a labeled LoCoMo/LongMemEval conversation (the headline number)
- [ ] Record the GIFs: `make eval` side-by-side (hero) + rollback (`docs/demo.tape`, vhs)

## Tuning targets
All knobs live in `mnemo/config.py` and are covered by tests; tune ONLY against
`make eval` (weights .4/.3/.3, cutoffs .70/.45, decay λ=0.16 / archive < 0.35,
rerank .5/.2/.3, γ=0.995/hr).
