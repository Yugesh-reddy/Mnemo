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


_SYSTEM_PROMPT = """Read the user's turn and extract its explicitly stated memory facts.
First copy the evidence, then express the SAME meaning as a subject/predicate/object.
Return only JSON: {"facts": [{"evidence": "verbatim source clause",
"subject": "user", "predicate": "precise_relation", "object": "complete value",
"confidence": 0.95, "importance": 7}]}.

For each independent assertion:
1. Copy a contiguous evidence span EXACTLY from the turn, including negation,
   tense, dates and qualifications. Do not rewrite or invent the quote.
2. subject is the actor/entity; use user for the speaker. predicate is a precise
   snake_case relationship actually expressed by that span. The vocabulary is open.
   object contains the complete value and any event context needed to preserve
   meaning. Keep what happened, to whom/what, where and when bound together.
   Put literal names, items and dates in the object, not inside the predicate.
   For stable attributes, the object is the value, not a repetition of the action.
3. Do not substitute another relationship. Using something is not preferring it.
   Liking a food is not an allergy. Using a programming language, appliance or
   oven setting does not make it an editor. Membership is not a job role.
   A person's manager is not someone that person manages. Listening often is
   frequency, not necessarily preference. Copy the stated attribute's meaning.
   Use specific known types: PostgreSQL is a database, Python is a programming
   language, and a mixer is an appliance. Database use is uses_database and an
   explicit database preference is preferred_database. Keep these distinct.
4. Include all explicit relevant facts, even when the turn also asks for advice:
   needs, preferences, tool use, projects, completed activities, learned skills,
   dates and corrections. A completed event must not become a generic present
   role or goal. An action's date belongs to that action, not to a different event.
5. Keep independent attributes separate. Preserve the same predicate through a
   correction: a meeting-free day remains meeting_free_day, and diet remains diet.
   Preserve all simultaneously mentioned values; a list is allowed when they share
   the exact same relation. Do not replace one value with a different concurrent one.
6. Preserve modality: thinking of/considering is considered_, explicit intention
   is planned_, and only stated completion is completed. Questions and denied or
   hypothetical positive facts are not assertions. Do not encode exclusions as
   null, not_X, or an affirmative replacement. Omit chit-chat and assistant claims.

importance is 1..10: high for important needs, identity, preferences and corrections;
moderate for relevant experiences and project context; low for fleeting trivia.
confidence is 0..1. If there are no supported memory facts, return {"facts": []}.
Do not fill imagined attributes or use predicate names as a menu.

Example user turn: "I bought a kiln in April. I might teach pottery next fall."
{"facts": [
 {"evidence": "I bought a kiln in April.", "subject": "user", "predicate": "purchased",
  "object": "kiln in April", "confidence": 0.95, "importance": 6},
 {"evidence": "I might teach pottery next fall.", "subject": "user",
  "predicate": "considered_teaching",
  "object": "pottery next fall", "confidence": 0.95, "importance": 5}
]}
Example user turn: "I use Postgres for my project."
{"facts": [{"evidence": "I use Postgres for my project.", "subject": "user",
"predicate": "uses_database", "object": "Postgres", "confidence": 0.95, "importance": 7}]}
"""


def _extraction_messages(
    text: str, role: str, validation_error: str | None, previous_content: str | None = None
) -> list[dict[str, str]]:
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Turn ({role}): {text}"},
    ]
    if validation_error:
        if previous_content is not None:
            messages.append({"role": "assistant", "content": previous_content})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Validator feedback (not source evidence): "
                    + validation_error
                    + ". Return the complete corrected facts array using only the original "
                    "Turn above. Copy evidence exactly and put the value in object."
                ),
            }
        )
    return messages


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
        last_error = None
        content = None
        for _ in range(self.max_retries + 1):
            content = self._chat(text, role, last_error, content)
            try:
                return self._parse(content, text)
            except ValueError as exc:
                last_error = str(exc)
        raise ValueError(f"extractor output rejected after retries: {last_error}")

    def _chat(
        self,
        text: str,
        role: str,
        validation_error: str | None = None,
        previous_content: str | None = None,
    ) -> str:
        resp = self._client.post(
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "messages": _extraction_messages(text, role, validation_error, previous_content),
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
    def _parse(content: str, source_text: str) -> list[ExtractedFact]:
        data = json.loads(content)
        if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
            raise ValueError("output requires a facts array")
        out: list[ExtractedFact] = []
        for index, item in enumerate(data["facts"]):
            fact = ExtractedFact.model_validate(item)
            if fact.evidence is None or fact.evidence not in source_text:
                raise ValueError(f"candidate {index} evidence is absent from source")
            if fact.object is None or (isinstance(fact.object, str) and not fact.object.strip()):
                raise ValueError(f"candidate {index} requires a nonempty value in object")
            out.append(fact)
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
        last_error = None
        content = None
        for _ in range(self.max_retries + 1):
            content = self._chat(text, role, last_error, content)
            try:
                return OllamaExtractor._parse(content, text)
            except ValueError as exc:
                last_error = str(exc)
        raise ValueError(f"extractor output rejected after retries: {last_error}")

    def _chat(
        self,
        text: str,
        role: str,
        validation_error: str | None = None,
        previous_content: str | None = None,
    ) -> str:
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": _extraction_messages(text, role, validation_error, previous_content),
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
            if cand.evidence is not None and cand.evidence not in text:
                result.update(reason="extracted evidence is absent from source", outcome="rejected")
            elif cand.confidence < self.settings.confidence_floor:
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
            base_span = {"turn_ids": [job["turn_id"]], "session_id": job["session_id"]}
            if job["cache_id"]:
                base_span["cache_ids"] = [str(job["cache_id"])]
            for index, item in enumerate(prepared):
                span = dict(base_span)
                # Only the trusted queued text determines evidence offsets. A
                # quote is a navigation aid, not a replacement for full-turn verification.
                quote = item["candidate"].get("evidence")
                if isinstance(quote, str) and quote and quote in text:
                    start = text.index(quote)
                    span["evidence"] = {
                        "text": quote,
                        "start": start,
                        "end": start + len(quote),
                        "offset_unit": "unicode_codepoint",
                    }
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
