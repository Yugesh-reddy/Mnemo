# Mnemo — build status (what's done vs what Claude Code finishes)

This starter implements the **Model-Council quality pipeline** on a runnable,
tested core. Hand it to Claude Code with PROJECT_SPEC.md + CLAUDE.md to productionize.

## Done here (runnable + tested)
- [x] Event-sourced store, append-only, with HEAD pointer (SQLite, Postgres-shaped)
- [x] Version ops: add, search, blame, revert, diff, log, commit  (M1–M2 substance)
- [x] Quality gate: salience-first scoring, importance(1–10), specificity, novelty
- [x] Verification gate (negation/hypothetical/assistant) — NLI stand-in
- [x] Dedup by canonical fact identity
- [x] "Demote, don't drop" tiering (durable/session/ephemeral) — protects recall
- [x] Ebbinghaus decay + recall reinforcement (S+1), reversible archive (not delete)
- [x] Reusable precision/recall eval harness (mnemo/eval.py) — the north star
- [x] Pluggable extractor (swap mock -> real LLM without touching the gate)
- [x] Production Postgres DDL (migrations/0001_init.sql)

## Claude Code completes (against PROJECT_SPEC.md)
- [ ] M3  swap lexical cosine -> real embeddings + pgvector; SQLite -> Postgres
- [ ] M4  swap mock extractor -> salience-first LLM call (structured output);
          swap heuristic verifier -> NLI entailment gatekeeper (>0.99) + LLM fallback;
          wire the two-tier fast-cache + invalidation handshake
- [ ] M4+ async consolidation/reflection job (episodic -> semantic)
- [ ] M5  MCP server (mnemo/mcp_server.py is scaffolded) + Python SDK polish
- [ ] M6  reference agent wired via MCP/SDK (examples/agent.py)
- [ ] M7  web UI: memory list/search, blame view, revert button, diff view
- [ ] M8  README polish + record the side-by-side junk-rate GIF (the launch asset)

## Tuning targets (start values in MODEL_COUNCIL_solution.md)
DUP_COSINE=0.90, EPHEMERAL_FLOOR=0.45, DURABLE_CUTOFF=0.70,
write-score weights .4/.3/.3, decay lambda_base=0.16. Tune for F1 on the eval.


## Revised build order (after round-4 review)
1. M2   core ops (event store solid)  ............................ DONE
2. M2.5 eval harness FIRST + baseline (mnemo/eval.py) ........... DONE (demo data; swap LoCoMo)
3. M3   extraction + Layers 0-3 (salience, regex, NLI, dedup) .. real LLM/NLI swap
4. M4   decay + tiering (Layer 5 before Layer 4) ............... mostly done; tune vs eval
5. M4.5 consolidation/reflection (Layer 4) LAST + trust-laundering safeguards

## Consolidation safeguards (when you build Layer 4)
- provenance='agent_reflection', default MEDIUM trust (never higher than sources)
- keep all source episodic event_ids in source_span so blame traces through
- run the NLI gate on each synthesized fact against ALL its source episodics
