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
from uuid import uuid4

import asyncpg
import httpx
from pydantic import ValidationError

from mnemo.audit import record_decision
from mnemo.config import Settings, get_settings
from mnemo.core import MnemoStore
from mnemo.db import to_vector_literal
from mnemo.models import ExtractedFact
from mnemo.quality import (
    build_verifier,
    is_transient,
    representation_error,
    specificity,
    tier_for,
    write_score,
)
from mnemo.telemetry import record_usage


@runtime_checkable
class Extractor(Protocol):
    """Turns a conversation turn into zero or more candidate facts."""

    def extract(self, text: str, role: str = "user") -> list[ExtractedFact]: ...


_SYSTEM_PROMPT = """Extract atomic memory assertions supported by the user's turn.

Return ONLY JSON of the form:
{"facts": [{"subject": "...", "predicate": "...", "object": "...",
            "kind": "triple", "confidence": 0.0-1.0, "importance": 1-10,
            "assertion_type": "..."}]}

Rules:
- Use a precise snake_case predicate for the relationship actually stated. The
  vocabulary is open. Do not force an unfamiliar relationship into role, timezone,
  goal, location or uses_database. Distinct attributes need distinct predicates.
- subject is the actual actor or entity. Use "user" for the speaker, but preserve
  named people, teams, services and projects when they are the subject.
- Preserve direction: "My manager is Dana" means user/manager/Dana, not that the
  user manages Dana. A team membership is not a job role.
- Preserve type: using an editor is editor use, not database use. A scheduled day
  is not a timezone. A need is not merely a generic goal. Use uses_database only
  for database use, and preferred_database only for an explicit database preference.
- Preserve numbers, times and complete corrected values. Keep independent facts
  separate, including accessibility needs, allergies and emergency contacts.
- Never encode an excluded value as a positive attribute: do not write name="not
  Dana", timezone="not UTC", object=null, or object={"not":...}. A denial supplies
  no positive replacement value. Leave its positive candidate out. A factual clause
  beside a denial or hypothetical still supports its own assertion.
- Distinguish plans from completed actions using planned_ or considered_ predicates.
  Do not turn a question into a fact. Extract all clearly stated, memory-relevant
  facts from multi-clause turns, even when the turn also asks for advice.
- importance: 1 (trivia/transient) to 10 (identity-defining durable fact).
- assertion_type is "direct_user_statement" when the user states it about themselves,
  otherwise "agent_inference".
- NEVER extract facts about the user from assistant turns.
- Include stable preferences, important needs, project context and explicit plans.
  Also include relevant completed activities, accomplishments and experiences.
  A question following a factual statement does not erase the stated fact. Do not
  replace a reported past event with a generic goal or a request for advice.
  Exclude chit-chat. If none, return {"facts": []}.

Examples:
Turn (user): "I prefer Postgres for my project."
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
        raise ValueError("extractor returned malformed output after retries")

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
                "think": False,
                "stream": False,
                "options": {"temperature": 0, "num_predict": 2048},
            },
        )
        resp.raise_for_status()
        record_usage(self, resp.json())
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
        if not isinstance(data.get("facts"), list):
            return None
        for item in data["facts"]:
            try:
                out.append(ExtractedFact.model_validate(item))
            except ValidationError:
                return None  # retry the batch rather than silently losing malformed candidates
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
        raise ValueError("extractor returned malformed output after retries")

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
        record_usage(self, resp.json())
        return resp.json()["choices"][0]["message"]["content"]

    def close(self) -> None:
        self._client.close()


class LeaseLost(RuntimeError):
    """A later claim owns the job; this attempt must not commit anything."""


class ExtractionWorker:
    """Fenced queue consumer. Each instance owns a dedicated connection.

    Scope is taken from each claimed job, never from constructor defaults.
    Model calls run outside transactions, with lease renewal on the event loop.
    """

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
        self.conn, self.embedder, self.extractor = conn, embedder, extractor
        self.settings = settings or get_settings()
        self.verifier = verifier or build_verifier(self.settings)
        self.actor = actor

    async def _claim(self) -> Any:
        async with self.conn.transaction():
            job = await self.conn.fetchrow(
                """
                SELECT * FROM extraction_job
                WHERE (status='pending' AND available_at <= clock_timestamp())
                   OR (status='processing' AND COALESCE(lease_expires_at,
                       updated_at + make_interval(secs => $1)) < clock_timestamp())
                ORDER BY created_at, job_id LIMIT 1 FOR UPDATE SKIP LOCKED
                """,
                self.settings.job_lease_seconds,
            )
            if job is None:
                return None
            if job["attempts"] >= self.settings.job_max_attempts:
                await self.conn.execute(
                    "UPDATE extraction_job SET status='failed', last_error='attempt limit', "
                    "locked_by=NULL, lease_expires_at=NULL, updated_at=clock_timestamp() "
                    "WHERE job_id=$1",
                    job["job_id"],
                )
                return {"exhausted": True}
            return await self.conn.fetchrow(
                """
                UPDATE extraction_job SET status='processing', attempts=attempts+1,
                  locked_by=$2, locked_at=clock_timestamp(), updated_at=clock_timestamp(),
                  lease_expires_at=clock_timestamp()+make_interval(secs => $3)
                WHERE job_id=$1 RETURNING *
                """,
                job["job_id"],
                uuid4(),
                self.settings.job_lease_seconds,
            )

    async def _renew(self, job: Any) -> None:
        renewed = await self.conn.fetchval(
            """UPDATE extraction_job
               SET lease_expires_at=clock_timestamp()+make_interval(secs => $3),
                   updated_at=clock_timestamp()
               WHERE job_id=$1 AND locked_by=$2 AND status='processing'
                 AND lease_expires_at > clock_timestamp() RETURNING job_id""",
            job["job_id"],
            job["locked_by"],
            self.settings.job_lease_seconds,
        )
        if renewed is None:
            raise LeaseLost("extraction lease expired or changed owner")

    async def _prepare(self, text: str, role: str) -> list[dict[str, Any]]:
        if role == "assistant":
            return [
                {"candidate": {}, "reason": "assistant turns are excluded", "outcome": "rejected"}
            ]
        raw = await asyncio.to_thread(self.extractor.extract, text, role)
        prepared = []
        for item in raw:
            try:
                cand = ExtractedFact.model_validate(item)
            except ValidationError:
                prepared.append(
                    {
                        "candidate": {"raw": str(item)},
                        "reason": "malformed candidate",
                        "outcome": "rejected",
                    }
                )
                continue
            result: dict[str, Any] = {"candidate": cand.model_dump(mode="json"), "fact": cand}
            if cand.confidence < self.settings.confidence_floor:
                result.update(reason="below extraction confidence floor", outcome="rejected")
            elif error := representation_error(cand, text):
                result.update(reason=error, outcome="rejected")
            else:
                verdict = await asyncio.to_thread(self.verifier.verify, cand, text)
                result["verification"] = verdict.model_dump(mode="json")
                if not verdict.accepted or verdict.label != "entailment":
                    result.update(reason=verdict.reason, outcome="rejected")
                else:
                    result["embedding"] = await asyncio.to_thread(
                        self.embedder.embed, f"{cand.subject} {cand.predicate} {cand.object}"
                    )
            prepared.append(result)
        if not prepared:
            prepared.append(
                {
                    "candidate": {},
                    "reason": "extractor found no atomic facts",
                    "outcome": "rejected",
                }
            )
        return prepared

    async def _prepare_with_renewal(self, job: Any, text: str, role: str) -> list[dict[str, Any]]:
        pending = asyncio.create_task(self._prepare(text, role))
        try:
            while not pending.done():
                done, _ = await asyncio.wait({pending}, timeout=self.settings.job_lease_seconds / 3)
                if not done:
                    await self._renew(job)
            result = await pending
            await self._renew(job)
            return result
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def _commit(self, job: Any, text: str, prepared: list[dict[str, Any]]) -> None:
        store = MnemoStore(
            self.conn,
            self.embedder,
            settings=self.settings,
            namespace=job["namespace"],
            user_id=job["user_id"],
            agent_id=job["agent_id"],
        )
        async with self.conn.transaction():
            await store._lock_scope()
            owned = await self.conn.fetchval(
                "SELECT job_id FROM extraction_job WHERE job_id=$1 AND locked_by=$2 "
                "AND status='processing' AND lease_expires_at > clock_timestamp() FOR UPDATE",
                job["job_id"],
                job["locked_by"],
            )
            if owned is None:
                raise LeaseLost("claim no longer owns the job at commit")
            first_event_id = None
            span = {"turn_ids": [job["turn_id"]], "session_id": job["session_id"]}
            if job["cache_id"]:
                span["cache_ids"] = [str(job["cache_id"])]
            for index, item in enumerate(prepared):
                event = None
                components = None
                outcome, reason = item.get("outcome"), item.get("reason")
                if outcome is None:
                    cand, emb = item["fact"], item["embedding"]
                    from mnemo.core import canonicalize

                    existing_id = await store._fact_id_for_key(
                        canonicalize(cand.subject, cand.predicate)
                    )
                    # A changed value under a known identity is a meaningful update.
                    # Similarity cannot remove its novelty or erase the correction.
                    novelty = (
                        1.0
                        if existing_id
                        else max(
                            0.0, min(1.0, 1.0 - await store._max_cosine(to_vector_literal(emb)))
                        )
                    )
                    components = dict(
                        importance=cand.importance,
                        spec=specificity(cand.predicate, self.settings.predicate_vocab),
                        novelty=novelty,
                        from_assistant=False,
                        transient=is_transient(text),
                    )
                    score = write_score(**components, settings=self.settings)
                    components["score"] = score
                    tier = tier_for(score, self.settings)
                    # Only clearly ephemeral candidates may be dropped for salience.
                    if tier is None and (existing_id or cand.importance > 2):
                        tier = "session"
                    if tier is None:
                        outcome, reason = "rejected", "ephemeral salience below floor"
                    else:
                        before_seq = await self.conn.fetchval(
                            "SELECT COALESCE(max(seq),0) FROM memory_event"
                        )
                        event = await store.add(
                            cand.subject,
                            cand.predicate,
                            cand.object,
                            kind=cand.kind,
                            provenance="agent_inference",
                            actor=self.actor,
                            confidence=cand.confidence,
                            source_span=span,
                            session_id=job["session_id"],
                            importance=cand.importance,
                            write_score=score,
                            tier=tier,
                            reason=f"entailment; score={score:.2f}",
                            embedding=emb,
                        )
                        outcome = (
                            "duplicate"
                            if event.seq <= before_seq
                            else ("demoted" if tier == "session" else "accepted")
                        )
                        reason = f"entailment; score={score:.2f}; {outcome}"
                        first_event_id = first_event_id or event.event_id
                await record_decision(
                    self.conn,
                    job=job,
                    candidate_index=index,
                    candidate=item["candidate"],
                    outcome=outcome,
                    reason=reason,
                    verification=item.get("verification"),
                    score_components=components,
                    source_span=span,
                    event_id=event.event_id if event else None,
                    settings=self.settings,
                )
            await self.conn.execute(
                """UPDATE fast_cache SET reconciled=true, reconciled_event_id=$1
                   WHERE namespace=$2 AND user_id=$3 AND agent_id=$4 AND session_id=$5
                     AND turn_id=$6 AND ($7::uuid IS NULL OR cache_id=$7) AND NOT reconciled""",
                first_event_id,
                job["namespace"],
                job["user_id"],
                job["agent_id"],
                job["session_id"],
                job["turn_id"],
                job["cache_id"],
            )
            await self.conn.execute(
                """UPDATE extraction_job SET status='done', updated_at=clock_timestamp(),
                   completed_at=clock_timestamp(), processing_ms=
                     EXTRACT(EPOCH FROM (clock_timestamp()-locked_at))*1000,
                   locked_by=NULL, lease_expires_at=NULL, last_error=NULL WHERE job_id=$1""",
                job["job_id"],
            )

    async def _retry(self, job: Any, exc: BaseException) -> None:
        delay = min(
            self.settings.job_retry_max_seconds,
            self.settings.job_retry_base_seconds * 2 ** min(job["attempts"] - 1, 16),
        )
        async with self.conn.transaction():
            owned = await self.conn.fetchval(
                """UPDATE extraction_job SET status=$3,
                       available_at=clock_timestamp()+make_interval(secs => $4),
                   updated_at=clock_timestamp(), locked_by=NULL, lease_expires_at=NULL,
                       last_error=$5
                   WHERE job_id=$1 AND locked_by=$2 AND status='processing' RETURNING job_id""",
                job["job_id"],
                job["locked_by"],
                "failed" if job["attempts"] >= self.settings.job_max_attempts else "pending",
                delay,
                type(exc).__name__,
            )
            if owned:
                await record_decision(
                    self.conn,
                    job=job,
                    candidate={},
                    outcome="error",
                    reason=type(exc).__name__,
                    settings=self.settings,
                )

    async def process_one(self) -> bool:
        job = await self._claim()
        if job is None:
            return False
        if "exhausted" in job:
            return True
        try:
            payload = (
                json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
            )
            text, role = payload["text"], payload.get("role", "user")
            prepared = await self._prepare_with_renewal(job, text, role)
            await self._commit(job, text, prepared)
        except asyncio.CancelledError as exc:
            await self._retry(job, exc)
            raise
        except LeaseLost:
            pass  # another owner will finish; never touch its claim or cache
        except Exception as exc:
            await self._retry(job, exc)
        return True

    async def run(
        self,
        *,
        poll_interval: float = 0.5,
        max_idle: int | None = None,
        stop: asyncio.Event | None = None,
    ) -> None:
        stop = stop or asyncio.Event()
        idle = 0
        while not stop.is_set():
            if await self.process_one():
                idle = 0
                continue
            idle += 1
            if max_idle is not None and idle >= max_idle:
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            except TimeoutError:
                pass


def build_extractor(settings: Settings | None = None) -> Extractor:
    """Construct the configured extractor (Ollama unless backend == 'openai')."""
    s = settings or get_settings()
    if s.backend == "openai":
        return OpenAIExtractor(
            model=s.extractor_model,
            api_key=s.openai_api_key,
            base_url=s.openai_base_url,
            max_retries=s.extractor_max_retries,
            timeout=s.extractor_timeout_seconds,
        )
    return OllamaExtractor(
        model=s.extractor_model,
        host=s.ollama_host,
        max_retries=s.extractor_max_retries,
        timeout=s.extractor_timeout_seconds,
    )
