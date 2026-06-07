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
from uuid import UUID

import asyncpg
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mnemo.config import get_settings
from mnemo.core import MnemoStore
from mnemo.db import register_vector
from mnemo.embedder import build_embedder

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.embedder = build_embedder(settings)
    app.state.pool = await asyncpg.create_pool(settings.dsn, init=register_vector)
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="Mnemo", lifespan=lifespan)


async def get_store(request: Request) -> AsyncIterator[MnemoStore]:
    async with request.app.state.pool.acquire() as conn:
        yield MnemoStore(conn, request.app.state.embedder, settings=request.app.state.settings)


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


@app.get("/fact/{fact_id}", response_class=HTMLResponse)
async def fact_detail(request: Request, fact_id: UUID, store: MnemoStore = Depends(get_store)):
    fact = await store.get(fact_id)
    history = await store.blame(fact_id=fact_id)
    return TEMPLATES.TemplateResponse(
        request,
        "fact.html",
        {"fact": fact, "history": list(reversed(history)), "fact_id": fact_id},
    )


@app.post("/fact/{fact_id}/revert/{event_id}")
async def do_revert(fact_id: UUID, event_id: UUID, store: MnemoStore = Depends(get_store)):
    await store.revert(fact_id, event_id)
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
