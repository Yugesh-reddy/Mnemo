"""Minimal web UI (FastAPI + HTMX + Tailwind) — spec §11.

Three views: memory list/search, fact detail (blame + revert + inline edit), and diff.
Server-rendered Jinja2; HTMX/Tailwind come from CDNs in the templates. Run: ``make ui``.

The ``get_store`` dependency hands each request a MnemoStore on a pooled connection;
tests override it to point at a disposable DB.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
from fastapi import Depends, FastAPI, Form, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mnemo.config import get_settings
from mnemo.core import MnemoStore
from mnemo.db import register_vector, validate_embedding_dimension
from mnemo.direct import DirectMemory
from mnemo.embedder import build_embedder
from mnemo.errors import ErrorCode, MnemoError

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.embedder = build_embedder(settings)
    pool = None

    async def init(conn: asyncpg.Connection) -> None:
        await register_vector(conn)
        await validate_embedding_dimension(conn, settings.embed_dim)

    try:
        pool = app.state.pool = await asyncpg.create_pool(settings.dsn, init=init)
        yield
    finally:
        if pool is not None:
            await pool.close()
        close = getattr(app.state.embedder, "close", None)
        if close:
            close()


app = FastAPI(title="Mnemo", lifespan=lifespan)


async def get_store(request: Request) -> AsyncIterator[MnemoStore]:
    async with request.app.state.pool.acquire() as conn:
        settings = request.app.state.settings
        yield MnemoStore(
            conn,
            request.app.state.embedder,
            settings=settings,
            namespace=settings.namespace,
            user_id=settings.user_id,
            agent_id=settings.agent_id,
        )


async def _facts(store: MnemoStore, q: str):
    return await store.search(q) if q.strip() else await store.list_current()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, q: str = "", store: MnemoStore = Depends(get_store)):
    facts = await _facts(store, q)
    return TEMPLATES.TemplateResponse(request, "list.html", {"facts": facts, "q": q})


@app.get("/rows", response_class=HTMLResponse)
async def rows(request: Request, q: str = "", store: MnemoStore = Depends(get_store)):
    facts = await _facts(store, q)
    return TEMPLATES.TemplateResponse(request, "_rows.html", {"facts": facts})


async def _fact_response(
    request: Request,
    fact_id: UUID,
    store: MnemoStore,
    *,
    message: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    # Render the value, history and expected revision from the same snapshot.
    async with store.conn.transaction(
        isolation=None if store.conn.is_in_transaction() else "repeatable_read"
    ):
        fact = await store.get(fact_id)
        history = await store.blame(fact_id=fact_id)
    head = next((e for e in history if fact and e.event_id == fact.event_id), None)
    request_ids = {
        event.event_id: uuid4()
        for event in history
        if DirectMemory._eligible(head)
        and DirectMemory._eligible(event)
        and event.event_id != head.event_id
    }
    return TEMPLATES.TemplateResponse(
        request,
        "fact.html",
        {
            "fact": fact,
            "history": list(reversed(history)),
            "fact_id": fact_id,
            "request_ids": request_ids,
            "message": message,
        },
        status_code=status_code,
    )


@app.get("/fact/{fact_id}", response_class=HTMLResponse)
async def fact_detail(request: Request, fact_id: UUID, store: MnemoStore = Depends(get_store)):
    return await _fact_response(request, fact_id, store)


@app.post("/fact/{fact_id}/revert/{event_id}")
async def do_revert(
    request: Request,
    fact_id: UUID,
    event_id: UUID,
    expected_event_id: UUID = Form(...),
    request_id: UUID = Form(...),
    store: MnemoStore = Depends(get_store),
):
    try:
        await DirectMemory(store).revert(
            fact_id,
            event_id,
            expected_event_id=expected_event_id,
            request_id=request_id,
            actor="ui",
        )
    except MnemoError as exc:
        status, message = {
            ErrorCode.REVISION_CONFLICT: (
                409,
                "This memory changed since you loaded the page — review and try again",
            ),
            ErrorCode.UNSUPPORTED_STATE: (
                409,
                "This memory or revision is unavailable for restore.",
            ),
            ErrorCode.REQUEST_ID_REUSED: (
                409,
                "This restore request was already used. Review the current memory and try again.",
            ),
            ErrorCode.NOT_FOUND: (404, "Memory not found."),
        }.get(exc.code, (400, exc.message))
        return await _fact_response(request, fact_id, store, message=message, status_code=status)
    return RedirectResponse(f"/fact/{fact_id}", status_code=303)


@app.post("/fact/{fact_id}/edit")
async def do_edit(fact_id: UUID, value: str = Form(...), store: MnemoStore = Depends(get_store)):
    fact = await store.get(fact_id)
    if fact is not None and value.strip():
        await store.add(
            fact.subject, fact.predicate, value.strip(), provenance="human_review", actor="ui"
        )
    return RedirectResponse(f"/fact/{fact_id}", status_code=303)


@app.get("/diff", response_class=HTMLResponse)
async def diff_view(
    request: Request, a: str = "", b: str = "", store: MnemoStore = Depends(get_store)
):
    commits = await store.list_commits()
    diff = await store.diff(UUID(a), UUID(b)) if a and b else None
    return TEMPLATES.TemplateResponse(
        request, "diff.html", {"commits": commits, "diff": diff, "a": a, "b": b}
    )


@app.post("/commit")
async def do_commit(label: str = Form(""), store: MnemoStore = Depends(get_store)):
    await store.commit(label.strip() or None)
    return RedirectResponse("/diff", status_code=303)


@app.get("/health")
async def health(store: MnemoStore = Depends(get_store)):
    from mnemo.audit import queue_health

    return await queue_health(
        store.conn, namespace=store.namespace, user_id=store.user_id, agent_id=store.agent_id
    )


@app.get("/decisions")
async def quality_decisions(
    turn_id: str | None = None, limit: int = 100, store: MnemoStore = Depends(get_store)
):
    from mnemo.audit import decisions

    return await decisions(
        store.conn,
        namespace=store.namespace,
        user_id=store.user_id,
        agent_id=store.agent_id,
        turn_id=turn_id,
        limit=limit,
    )


@app.get("/operations", response_class=HTMLResponse)
async def operations(request: Request, turn_id: str = "", store: MnemoStore = Depends(get_store)):
    return TEMPLATES.TemplateResponse(
        request,
        "operations.html",
        {
            "health": jsonable_encoder(await health(store)),
            "decisions": jsonable_encoder(await quality_decisions(turn_id or None, 100, store)),
            "turn_id": turn_id,
        },
    )
