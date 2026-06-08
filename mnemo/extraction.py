"""Async extraction pipeline (spec §6): extract → reconcile → handshake.

The extractor is an *untrusted* actor: its output is validated against a Pydantic
schema, retried on malformed JSON (max 2), and low-confidence candidates are dropped.
The worker then reconciles each survivor via ``MnemoStore.add`` and performs the
cache-invalidation handshake atomically with the writes (spec §8).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol, runtime_checkable

import asyncpg
import httpx
from pydantic import ValidationError

from mnemo.config import Settings, get_settings
from mnemo.core import MnemoStore
from mnemo.db import to_vector_literal
from mnemo.models import ExtractedFact
from mnemo.quality import HeuristicVerifier, is_transient, specificity, tier_for, write_score

# Only explicit user assertions are high-trust; everything else is agent inference.
ASSERTION_TO_PROVENANCE: dict[str, str] = {
    "direct_user_statement": "direct_user_statement",
    "tool_output": "tool_output",
    "document": "document",
    "human_review": "human_review",
    "agent_inference": "agent_inference",
}


@runtime_checkable
class Extractor(Protocol):
    """Turns a conversation turn into zero or more candidate facts."""

    def extract(self, text: str, role: str = "user") -> list[ExtractedFact]: ...


_SYSTEM_PROMPT = """You extract durable facts from a conversation turn.

Return ONLY JSON of the form:
{"facts": [{"subject": "...", "predicate": "...", "object": "...",
            "kind": "triple", "confidence": 0.0-1.0, "importance": 1-10,
            "assertion_type": "..."}]}

Rules:
- Prefer these predicates when they fit: preferred_database, preferred_language,
  name, location, role, timezone, goal. Use a concise snake_case predicate otherwise.
- subject is usually "user".
- importance: 1 (trivia/transient) to 10 (identity-defining durable fact).
- assertion_type is "direct_user_statement" when the user states it about themselves,
  otherwise "agent_inference".
- NEVER extract facts about the user from assistant turns.
- Only durable facts about the user/project. No chit-chat. If none, return {"facts": []}.

Examples:
Turn (user): "I use Postgres for my project."
{"facts": [{"subject": "user", "predicate": "preferred_database", "object": "PostgreSQL",
            "kind": "triple", "confidence": 0.97, "importance": 8,
            "assertion_type": "direct_user_statement"}]}
Turn (user): "Thanks, that helps!"
{"facts": []}
"""


class OllamaExtractor:
    """Structured-output extraction via a local Ollama instruct model."""

    def __init__(
        self,
        *,
        model: str = "llama3.2:3b",
        host: str = "http://localhost:11434",
        max_retries: int = 2,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout)

    def extract(self, text: str, role: str = "user") -> list[ExtractedFact]:
        for _ in range(self.max_retries + 1):
            content = self._chat(text, role)
            facts = self._parse(content)
            if facts is not None:
                return facts
        return []  # gave up after retries: drop rather than store junk

    def _chat(self, text: str, role: str) -> str:
        resp = self._client.post(
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"Turn ({role}): {text}"},
                ],
                "format": "json",
                "stream": False,
                "options": {"temperature": 0},
            },
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    @staticmethod
    def _parse(content: str) -> list[ExtractedFact] | None:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        out: list[ExtractedFact] = []
        for item in data.get("facts", []):
            try:
                out.append(ExtractedFact.model_validate(item))
            except ValidationError:
                continue  # drop the malformed item, keep the rest
        return out

    def close(self) -> None:
        self._client.close()


class OpenAIExtractor:
    """Structured-output extraction via the OpenAI chat completions API."""

    def __init__(
        self,
        *,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        max_retries: int = 2,
        timeout: float = 120.0,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI backend requires an API key (set OPENAI_API_KEY)")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"})

    def extract(self, text: str, role: str = "user") -> list[ExtractedFact]:
        for _ in range(self.max_retries + 1):
            content = self._chat(text, role)
            facts = OllamaExtractor._parse(content)
            if facts is not None:
                return facts
        return []

    def _chat(self, text: str, role: str) -> str:
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"Turn ({role}): {text}"},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def close(self) -> None:
        self._client.close()


class ExtractionWorker:
    """Drains extraction_job: claim → extract → reconcile + handshake (atomic)."""

    def __init__(
        self,
        conn: asyncpg.Connection,
        embedder: Any,
        extractor: Extractor,
        verifier: Any | None = None,
        *,
        settings: Settings | None = None,
        namespace: str = "default",
        user_id: str = "default",
        agent_id: str = "default",
        actor: str = "extractor",
    ) -> None:
        self.conn = conn
        self.embedder = embedder
        self.extractor = extractor
        self.verifier = verifier or HeuristicVerifier()
        self.settings = settings or get_settings()
        self.namespace = namespace
        self.user_id = user_id
        self.agent_id = agent_id
        self.actor = actor

    async def process_one(self) -> bool:
        """Process a single job. Returns False if the queue is empty.

        Claims a ``pending`` job, or a ``processing`` job whose lease expired (the
        owning worker died mid-extraction). Jobs past ``job_max_attempts`` are marked
        ``failed`` rather than retried forever.
        """
        async with self.conn.transaction():
            # Reclaim stale 'processing' rows too: a worker that died after claiming
            # would otherwise leave the job stranded (the original claim released the
            # txn before the network-bound extraction ran). The lease interval is built
            # in SQL from a numeric arg (asyncpg binds interval params as timedelta).
            job = await self.conn.fetchrow(
                """
                SELECT job_id, attempts FROM extraction_job
                WHERE status = 'pending'
                   OR (status = 'processing' AND updated_at < now() - make_interval(secs => $1))
                ORDER BY created_at, job_id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                float(self.settings.job_lease_seconds),
            )
            if job is None:
                return False

            # Past the attempt cap: give up permanently rather than loop forever.
            if job["attempts"] >= self.settings.job_max_attempts:
                await self.conn.execute(
                    "UPDATE extraction_job SET status='failed', updated_at=now() WHERE job_id=$1",
                    job["job_id"],
                )
                return True  # there may be more jobs; keep draining

            # Re-fetch the full row now that we know we're proceeding.
            job = await self.conn.fetchrow(
                """
                SELECT job_id, turn_id, session_id, payload FROM extraction_job
                WHERE job_id=$1
                """,
                job["job_id"],
            )
            await self.conn.execute(
                "UPDATE extraction_job SET status='processing', attempts=attempts+1, "
                "updated_at=now() WHERE job_id=$1",
                job["job_id"],
            )

        payload = job["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        text = payload.get("text", "")
        role = payload.get("role", "user")

        try:
            candidates = await asyncio.to_thread(self.extractor.extract, text, role)
        except Exception:
            await self.conn.execute(
                "UPDATE extraction_job SET status='failed', updated_at=now() WHERE job_id=$1",
                job["job_id"],
            )
            raise  # fail loud

        async with self.conn.transaction():
            store = MnemoStore(
                self.conn,
                self.embedder,
                namespace=self.namespace,
                user_id=self.user_id,
                agent_id=self.agent_id,
                settings=self.settings,
            )
            first_event_id = None
            for cand in candidates:
                if cand.confidence < self.settings.confidence_floor:
                    continue  # malformed/low-confidence junk (Layer 0 floor)

                # Layer 1: verification — no false memories from negation/hypothetical.
                verdict = self.verifier.verify(str(cand.object), text)
                if not verdict.accepted:
                    continue

                # Layer 2: salience score + tier.
                emb = await asyncio.to_thread(
                    self.embedder.embed, f"{cand.subject} {cand.predicate} {cand.object}"
                )
                novelty = 1.0 - await store._max_cosine(to_vector_literal(emb))
                score = write_score(
                    importance=cand.importance,
                    spec=specificity(cand.predicate, self.settings.predicate_vocab),
                    novelty=novelty,
                    from_assistant=(role == "assistant"),
                    transient=is_transient(text),
                    settings=self.settings,
                )
                tier = tier_for(score, self.settings)
                if tier is None:
                    continue  # true ephemeral noise: below the salience floor

                provenance = ASSERTION_TO_PROVENANCE.get(cand.assertion_type, "agent_inference")
                event = await store.add(
                    cand.subject,
                    cand.predicate,
                    cand.object,
                    kind=cand.kind,
                    provenance=provenance,
                    actor=self.actor,
                    confidence=cand.confidence,
                    source_span={"turn_ids": [job["turn_id"]]},
                    session_id=job["session_id"],
                    importance=cand.importance,
                    write_score=score,
                    tier=tier,
                    reason=f"{verdict.label}; score={score:.2f}",
                    embedding=emb,
                )
                first_event_id = first_event_id or event.event_id

            # Handshake: drop the raw cache row in favour of the semantic fact.
            await self.conn.execute(
                "UPDATE fast_cache SET reconciled=true, reconciled_event_id=$1 "
                "WHERE namespace=$2 AND session_id=$3 AND turn_id=$4 AND reconciled = false",
                first_event_id,
                self.namespace,
                job["session_id"],
                job["turn_id"],
            )
            await self.conn.execute(
                "UPDATE extraction_job SET status='done', updated_at=now() WHERE job_id=$1",
                job["job_id"],
            )
        return True

    async def run(self, *, poll_interval: float = 0.5, max_idle: int | None = None) -> None:
        """Drain continuously; stop after ``max_idle`` empty polls if set."""
        idle = 0
        while True:
            if await self.process_one():
                idle = 0
                continue
            idle += 1
            if max_idle is not None and idle >= max_idle:
                return
            await asyncio.sleep(poll_interval)


def build_extractor(settings: Settings | None = None) -> Extractor:
    """Construct the configured extractor (Ollama unless backend == 'openai')."""
    s = settings or get_settings()
    if s.backend == "openai":
        return OpenAIExtractor(
            model=s.extractor_model, api_key=s.openai_api_key, base_url=s.openai_base_url
        )
    return OllamaExtractor(model=s.extractor_model, host=s.ollama_host)
