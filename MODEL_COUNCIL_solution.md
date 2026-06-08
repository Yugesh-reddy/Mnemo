# Model Council: The Memory-Quality Solution for Mnemo

> **What this is (honest framing):** a structured deliberation across five expert
> viewpoints, each grounded in real, cited literature, converging on one
> build-ready architecture. It is rigorous multi-perspective reasoning — not
> literally separate AI models conferring. Sources are attributed inline.

## The question on the table
Agent memory stores ~96–98% junk, invents false facts (negations, hypotheticals),
and buries the important ones (ref: Mem0 issue #4573 — 97.8% of 10,134 entries junk).
Mnemo already has an append-only, bitemporal, event-sourced store on Postgres with
fact identity, provenance, confidence, and trust. **Design a write-time + consolidation
pipeline that maximizes precision (stored = useful) AND recall (important facts kept) —
without trading junk for amnesia, and without rework to the existing store.**

---

## The council

- **Dr. M — memory-systems researcher.** Generative Agents, MemoryBank, A-MEM, MemGPT.
- **E — extraction / NLP engineer.** FActScore, FactEHR, NLI verification, constrained decoding.
- **P — production data engineer.** Latency/cost, event-store integration, ops.
- **C — cognitive-science voice.** Forgetting curves, consolidation, recall reinforcement.
- **S — skeptical PM.** Guards the precision/recall tradeoff; "ship the smallest thing that proves it."

---

## Round 1 — Root cause (consensus)
Junk is not one bug; it's five missing stages, all upstream of storage:
1. **Indiscriminate extraction** — "extract facts" pulls transient state, pleasantries, the assistant's own words.
2. **No verification** — negations and hypotheticals become confident false facts.
3. **No salience** — everything is stored as equally durable.
4. **No dedup** — the same fact lands 50 times in slightly different forms.
5. **No forgetting** — stale facts accumulate forever.

> **S:** "So the fix isn't a smarter vector store. It's a *pipeline* on the write path. Good — that's buildable on what we have."

---

## Round 2 — The layered solution

### Layer 0 — Salience-first extraction (E leads)
> **E:** Don't ask for "facts." Use **fact decomposition** the way FActScore/FactEHR do — atomic, single-fact statements, *explicitly excluding* conversational acknowledgments and empathy. Their canonical example: from *"I'm sorry you're worried. Your PSA is 4.2"* extract only *"PSA is 4.2"* (Northeastern/Stanford, FactEHR, 2024).
> Add two fields at generation time, in the same structured-output call:
> - **importance ∈ [1,10]** — Generative Agents (Park et al., UIST 2023) assigns this with the LLM at creation; mundane → low, meaningful → high.
> - **tier ∈ {durable, session, ephemeral}** — classify lifespan up front.
> Constrain `predicate` to a controlled vocabulary; never extract user-facts from the assistant's turns (tag them `agent_inference`).

> **P:** Fine, but that's one LLM call per turn. Acceptable if it's a cheap model and async — which our two-tier fast-cache already allows.

### Layer 1 — Verification gate (E + Dr. M)
> **E:** This is the layer that kills false memories. Run **NLI entailment** of each candidate against its source turn: classify `entailment | contradiction | neutral`, accept **only entailment** (the KGHaluBench / FactEHR pattern, 2024–26). Negation → contradiction. Hypothetical → neutral. Sarcasm → usually neutral. All rejected, automatically, by construction.
> **Cost control (Deep-Research-agent pattern, 2026):** use a small NLI model as a gatekeeper, auto-accept only at extreme confidence (>0.99 entailment), and delegate the ambiguous minority to a cheap LLM. Most candidates never touch the expensive model.

> **Dr. M:** And this generalizes the negation/hypothetical heuristics into one principled check. Keep cheap regex heuristics as a *fast pre-filter* (free), NLI/LLM only for what passes.

### Layer 2 — Scoring + tiering gate (Dr. M + S)
> **Dr. M:** Compute a **write-score** before storage:
> `write_score = w_imp·(importance/10) + w_spec·specificity + w_nov·novelty − w_src·assistant_penalty`
> where specificity = predicate-in-vocab, novelty = `1 − max cosine to existing facts`.
> Map to tier; **drop only true ephemeral noise.**

> **S (the non-negotiable):** **Demote, don't drop.** Borderline facts go to `session`/low-trust/short-TTL, *never* deleted. We have TWO failure modes — stores junk AND misses critical — and a naive high threshold fixes the first by worsening the second. Demote-don't-drop + the append-only log (reversible) is what protects recall. Store the score, the reason, and provenance **on every event** so it's auditable, not a black box.

### Layer 3 — Dedup / entity resolution (P)
> **P:** Collapse near-duplicates and resolve `favorite_db` ≡ `preferred_database` to one `fact_id` via (a) canonicalized `subject|predicate|key` and (b) embedding similarity. Threshold ≈ 0.9 cosine → route to **UPDATE** the existing fact (an event), not ADD a new one. Use an LLM-as-judge merge **only** for the genuinely ambiguous middle band — not every write.

### Layer 4 — Async consolidation + reflection (C + Dr. M)
> **C:** Periodically run **reflection/consolidation** (Generative Agents): cluster related episodic facts and synthesize semantic ones. The textbook case — *"user corrected the date format on Jan 5, Jan 12, Feb 1" → "user prefers DD/MM/YYYY."* Most systems never do this automatically; doing it is a differentiator. Trigger on a cadence (every N events) or when a cluster crosses a size threshold.

> **Dr. M:** Consolidation also *raises precision retroactively* — it replaces many low-value episodics with one high-value semantic fact.

### Layer 5 — Principled forgetting / decay (C)
> **C:** Don't let durable memory grow unbounded. Use the **Ebbinghaus** model from MemoryBank (Zhong et al., 2024): `R = e^(−t/S)`, S starts at 1 and **+1 on every recall, resetting t** — so frequently used facts strengthen, unused ones fade. Modulate by importance (the 2026 production variant): `λ_eff = 0.16 × (1 − importance × 0.8)`, so important facts decay slower.
> **Crucial, per S:** decay = **demote priority + archive**, never hard-delete. The event store makes "forgetting" reversible — a wrongly-faded fact can be restored. (Biologically faithful too: FadeMem, 2026, uses layer-dependent decay shapes — sub-linear for long-term, super-linear for short-term.)

### Measurement (S owns it)
> **S:** None of this is real until it's measured. Build a **precision/recall junk-rate eval**: feed a labeled conversation, report precision (fraction of stored that's useful), recall (fraction of must-keep facts retained), and false-fact count — for the gated pipeline vs a naive baseline (and vs Mem0 on LoCoMo/LongMemEval). *That number is the proof, the differentiator, and the demo.*

---

## Round 3 — Disagreements, resolved
- **E vs P (cost):** "NLI on every write is too slow." → Heuristic pre-filter (free) → cheap NLI gatekeeper (auto-accept >0.99) → LLM only for the ambiguous minority. Run the whole thing async behind the fast-cache.
- **Dr. M vs S (forgetting):** "Aggressive decay loses facts." → Decay **demotes + archives**, never destroys; recall **reinforces** (S+1). Precision *and* recall are measured, so over-forgetting shows up immediately.
- **S vs everyone (black-box scoring):** "Importance/score can't be opaque." → Persist `importance`, `write_score`, `reason`, `provenance`, `trust_level` on each event. Mnemo's existing provenance/trust model already carries this — it becomes the audit trail for *why a memory was kept, demoted, or dropped*.

---

## The converged architecture (build this)

**Write path (per turn, async behind the fast-cache):**
1. **Extract** — salience-first fact decomposition; exclude conversational/empathy; emit `{subject, predicate, object, kind, importance∈[1,10], tier_guess, assertion_type}`; controlled predicate vocab; never user-facts from assistant turns.
2. **Pre-filter (free)** — regex heuristics drop obvious negation / hypothetical / pure-transient before any model call.
3. **Verify** — NLI entailment vs source turn; accept only `entailment` (cheap NLI gatekeeper >0.99; LLM for ambiguous). Reject `contradiction`/`neutral`.
4. **Dedup / resolve** — canonical `fact_key` + embedding match (≈0.9) → UPDATE existing `fact_id`, else new fact.
5. **Score + tier** — `write_score`; **demote borderline, drop only ephemeral noise**.
6. **Commit** — append-only event with `importance, write_score, tier, provenance, trust, reason`.

**Background jobs:**
7. **Consolidation/reflection** — cluster episodics → synthesize semantic facts (every N events / on cluster size).
8. **Decay/reinforce** — Ebbinghaus `R=e^(−t/S)`, importance-modulated; recall → S+1, t→0; below threshold → **archive (reversible), not delete**.

**Retrieval (Generative Agents composite):**
`score = α_rec·recency + α_rel·relevance + α_imp·(importance/10)`, recency = exp decay (γ≈0.995/hr), relevance = embedding cosine; filter by tier + bitemporal validity.

### Parameter starting points (tune against the eval)
| Knob | Start | Source / rationale |
|---|---|---|
| importance scale | 1–10, LLM-assigned | Generative Agents (Park 2023) |
| NLI auto-accept | entailment p > 0.99 | Deep-Research-agent gatekeeper (2026) |
| dedup → UPDATE | cosine ≥ 0.90 | common practice; Mnemo spec |
| write-score weights | w_imp .4 / w_spec .3 / w_nov .3 | start equal-ish, tune for F1 |
| tier: durable | write_score ≥ 0.7 | demote below, don't drop |
| decay | R=e^(−t/S), S+1 on recall | MemoryBank (Zhong 2024) |
| importance-mod decay | λ_eff = 0.16·(1−imp·0.8) | production variant (2026) |
| consolidation trigger | every 50 events / cluster ≥ 3 | Generative Agents reflection cadence |
| retrieval recency γ | 0.995 / hour | Generative Agents |

---

## Why this beats the incumbents (and is honest about the moat)
- It attacks the **loud, validated** pain (junk/false memories), not the niche one (versioning).
- It's a **standalone, measurable, drop-in** quality gate over *any* store — not one vendor's internal fix.
- It uniquely keeps the whole thing **auditable and reversible** (the event log): every keep/demote/drop/forget is explained and undoable. That's Mnemo's existing differentiator, now doing real work.
- **Honest caveat:** every individual layer here is established technique (importance scoring, NLI verification, Ebbinghaus decay, reflection). The contribution is *assembling them into one principled, measured, reversible pipeline and proving the precision/recall number* — not inventing a new algorithm. You're competing with Mem0 on extraction quality, so the win has to be the measurement + the reversibility + the packaging, not "we extract better" alone.

## What's a heuristic vs worth a cheap LLM call
- **Heuristic / free:** canonicalization, the regex pre-filter (negation/hypothetical/transient), specificity (vocab lookup), novelty (cosine), the write-score, tiering, the decay math.
- **Cheap LLM / NLI (async only):** the salience-first extraction itself, the NLI entailment verdict on the ambiguous minority, and the LLM-as-judge merge for ambiguous dedup.
- This keeps the hot path free and the model spend on the few decisions that actually need judgment.

---

## Revisions after review (round 4)
Three technical catches, all accepted, plus a build-order change. These supersede the earlier text where they conflict.

### Consolidation must not become a trust launderer (critical fix)
The async consolidation/reflection layer (Layer 4) is *itself a write path* and must pass the same gates — otherwise a hallucinated synthesized fact becomes a high-trust durable false memory with no source turn to blame. Hardening:
- Tag consolidated facts `provenance = agent_reflection`, default **medium** trust (never higher than their sources).
- Keep **every** originating episodic `event_id` in `source_span`, so `blame` traces through the consolidation back to the raw turns.
- Run the NLI entailment gate on each synthesized fact against **all** its source episodics; accept only if entailed by that evidence.
- Result: consolidation stays auditable and reversible like every other event — not a way to launder unverified claims into durable memory.

### Retrieval composite = rerank top-k, not a custom index
Compute `α_rec·recency + α_rel·relevance + α_imp·importance` as a **rerank over the HNSW top-k**, not baked into the index. Recency and importance are stored on the event (cheap); relevance is the existing vector search. Do not build a custom retrieval index until the write path is proven.

### Eval-first (the S voice, promoted to rule #1)
The write-score weights (.4/.3/.3), tier thresholds, and decay constants are **unvalidated priors**. Build the precision/recall eval harness against a labeled dataset FIRST, establish the naive baseline, then tune every Layer-2/Layer-5 knob against it. The number is the north star and the demo — not the blame UI or the diff view.

### Revised milestone order
1. **M2** — core ops (event store solid).
2. **M2.5 — eval harness FIRST** (precision/recall vs naive baseline on a labeled ~200-turn conversation; tune everything against this).
3. **M3** — extraction + Layers 0–3 (salience decomposition, regex pre-filter, NLI gate, dedup) — biggest precision impact, most predictable cost.
4. **M4** — decay + tiering (Layer 5 *before* Layer 4; Ebbinghaus is simpler to ship than reflection).
5. **M4.5** — consolidation/reflection (Layer 4) **LAST** — riskiest; add only once the eval shows episodic bloat is costing precision, and only with the trust-laundering safeguards above.

**Honest note on numbers:** any specific figures quoted for this design (e.g. "78% precision vs Mem0 2.2%") are *illustrative placeholders* to convey the shape of the win, not measured results. The architecture is ready to produce a real number on LoCoMo/LongMemEval — measuring it is the next move, and it will be either impressive or sobering.
