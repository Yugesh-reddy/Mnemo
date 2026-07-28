"""Minimal web UI (FastAPI + HTMX + Tailwind) — spec §11.

Three views: memory list/search, fact detail (blame + revert + inline edit), and diff.
Server-rendered Jinja2 with bundled htmx and a prebuilt Tailwind stylesheet in
``mnemo/web/static`` (``make css`` rebuilds it), so the UI works offline. Run: ``make ui``.

The ``get_store`` dependency opens the store file for each request and closes it
after; tests override it to point at a disposable store.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mnemo.config import get_settings
from mnemo.core import MnemoStore
from mnemo.db import check_schema, connect
from mnemo.direct import DirectMemory
from mnemo.embedder import build_embedder
from mnemo.errors import ErrorCode, MnemoError

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.embedder = build_embedder(settings)
    try:
        yield
    finally:
        close = getattr(app.state.embedder, "close", None)
        if close:
            close()


app = FastAPI(title="Mnemo", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Refuse a write that a browser sends from another site's page (cross-site request
    forgery against the local UI). Browsers set Origin on cross-site form posts;
    requests without it (curl, tests) are not from a web page and pass."""
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin is not None:
        host = request.headers.get("host", "")
        if origin.split("://", 1)[-1] != host:
            return HTMLResponse("Cross-site request refused.", status_code=403)
    return await call_next(request)


async def get_store(request: Request) -> AsyncIterator[MnemoStore]:
    settings = request.app.state.settings
    conn = await connect(settings.db, create=False)
    try:
        await check_schema(conn)
        yield MnemoStore(
            conn,
            request.app.state.embedder,
            settings=settings,
            namespace=settings.namespace,
            user_id=settings.user_id,
            agent_id=settings.agent_id,
        )
    finally:
        await conn.close()


async def _facts(store: MnemoStore, q: str):
    # Browsing is not recall: a person's search leaves the recall counters alone.
    return await store.search(q, reinforce=False) if q.strip() else await store.list_current()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, q: str = "", store: MnemoStore = Depends(get_store)):
    facts = await _facts(store, q)
    return TEMPLATES.TemplateResponse(request, "list.html", {"facts": facts, "q": q})


@app.get("/retracted", response_class=HTMLResponse)
async def retracted(request: Request, store: MnemoStore = Depends(get_store)):
    items = await DirectMemory(store).retracted()
    return TEMPLATES.TemplateResponse(request, "retracted.html", {"items": items})


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
    async with store.conn.transaction(readonly=not store.conn.is_in_transaction()):
        fact = await store.get(fact_id)
        history = await store.blame(fact_id=fact_id)
        retracted = None
        if fact is None and history:
            try:  # a retracted memory is hidden from store.get but can be restored
                retracted = await DirectMemory(store).get(fact_id)
            except MnemoError:
                pass
    head_id = fact.event_id if fact else retracted.current_event_id if retracted else None
    head = next((e for e in history if e.event_id == head_id), None)
    request_ids = {
        event.event_id: uuid4()
        for event in history
        if (DirectMemory._eligible(head) or retracted is not None)
        and DirectMemory._eligible(event)
        and event.event_id != head_id
    }
    return TEMPLATES.TemplateResponse(
        request,
        "fact.html",
        {
            "fact": fact,
            "retracted": retracted,
            "head_id": head_id,
            "history": list(reversed(history)),
            "fact_id": fact_id,
            "request_ids": request_ids,
            "correction_id": uuid4(),
            # The guarded update accepts only durable memories; others get no form.
            "correctable": fact is not None and DirectMemory._eligible(head),
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
        return await _refused(request, fact_id, store, exc, "restore")
    return RedirectResponse(f"/fact/{fact_id}", status_code=303)


@app.post("/fact/{fact_id}/edit")
async def do_edit(
    request: Request,
    fact_id: UUID,
    value: str = Form(...),
    expected_event_id: UUID = Form(...),
    request_id: UUID = Form(...),
    store: MnemoStore = Depends(get_store),
):
    """A guarded correction: it applies only to the revision the page showed, so an
    agent's write made while the page was open is never silently overwritten."""
    if not value.strip():
        return await _fact_response(
            request, fact_id, store, message="Enter a corrected value.", status_code=400
        )
    try:
        await DirectMemory(store).correct(
            fact_id,
            value.strip(),
            expected_event_id=expected_event_id,
            request_id=request_id,
        )
    except MnemoError as exc:
        return await _refused(request, fact_id, store, exc, "correction")
    return RedirectResponse(f"/fact/{fact_id}", status_code=303)


async def _refused(
    request: Request, fact_id: UUID, store: MnemoStore, exc: MnemoError, action: str
) -> HTMLResponse:
    """Re-render the memory as it is now, saying why the write was refused."""
    status, message = {
        ErrorCode.REVISION_CONFLICT: (
            409,
            f"This memory changed since you loaded the page, so your {action} was not "
            "saved. Review the current value and try again.",
        ),
        ErrorCode.UNSUPPORTED_STATE: (409, f"This memory is unavailable for {action}."),
        ErrorCode.REQUEST_ID_REUSED: (
            409,
            f"This {action} request was already used. Review the current memory and try again.",
        ),
        ErrorCode.NOT_FOUND: (404, "Memory not found."),
    }.get(exc.code, (400, exc.message))
    return await _fact_response(request, fact_id, store, message=message, status_code=status)


@app.get("/diff", response_class=HTMLResponse)
async def diff_view(
    request: Request, a: str = "", b: str = "", store: MnemoStore = Depends(get_store)
):
    commits = await store.list_commits()
    diff, message = None, None
    if a and b:
        try:
            diff = await store.diff(UUID(a), UUID(b))
        except ValueError:  # not a UUID, or not a commit in this scope
            message = "Pick two snapshots from the lists below."
    return TEMPLATES.TemplateResponse(
        request,
        "diff.html",
        {"commits": commits, "diff": diff, "a": a, "b": b, "message": message},
        status_code=400 if message else 200,
    )


@app.post("/commit")
async def do_commit(label: str = Form(""), store: MnemoStore = Depends(get_store)):
    await store.commit(label.strip() or None)
    return RedirectResponse("/diff", status_code=303)
