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
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
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


@dataclass(frozen=True)
class ScopeOption:
    value: str
    label: str
    url: str


def _scope_label(namespace: str, default: str) -> str:
    """Human-readable label for a namespace: 'global' for the base lane, project name/path
    for '<base>@<project>', else the raw namespace."""
    if namespace == default:
        return "global" if default == "default" else f"global ({default})"
    prefix = f"{default}@"
    if namespace.startswith(prefix):
        project = namespace[len(prefix) :]
        return project.removeprefix("path:")
    return namespace


def _scope_url(path: str, scope: str, default_scope: str, **params: str) -> str:
    query: list[tuple[str, str]] = []
    if scope != default_scope:
        query.append(("scope", scope))
    for key, value in params.items():
        if value:
            query.append((key, value))
    return f"{path}?{urlencode(query)}" if query else path


def _scoped_store(base: MnemoStore, namespace: str) -> MnemoStore:
    if namespace == base.namespace:
        return base
    return MnemoStore(
        base.conn,
        base.embedder,
        namespace=namespace,
        user_id=base.user_id,
        agent_id=base.agent_id,
        settings=base.settings,
    )


async def _known_scopes(store: MnemoStore) -> list[str]:
    """Distinct namespaces present in this store for the configured user/agent, default first."""
    rows = await store.conn.fetch(
        """
        SELECT DISTINCT namespace FROM memory_fact
        WHERE user_id = ?1 AND agent_id = ?2
        UNION
        SELECT DISTINCT namespace FROM memory_commit
        WHERE user_id = ?1 AND agent_id = ?2
        ORDER BY namespace
        """,
        store.user_id,
        store.agent_id,
    )
    prefix = f"{store.namespace}@"
    others = [row["namespace"] for row in rows if row["namespace"] != store.namespace]
    others.sort(
        key=lambda ns: (not ns.startswith(prefix), _scope_label(ns, store.namespace).lower(), ns)
    )
    return [store.namespace, *others]


async def _fact_namespace(store: MnemoStore, fact_id: UUID) -> str | None:
    """The namespace holding ``fact_id`` for this user/agent, or None if not found."""
    return await store.conn.fetchval(
        "SELECT namespace FROM memory_fact WHERE fact_id=?1 AND user_id=?2 AND agent_id=?3",
        fact_id,
        store.user_id,
        store.agent_id,
    )


def _scope_context(
    known: list[str], active: str, default_scope: str, *, switch_target: str
) -> dict[str, Any]:
    return {
        "scope": active,
        "default_scope": default_scope,
        "scope_label": _scope_label(active, default_scope),
        "scopes": [
            ScopeOption(
                value=ns,
                label=_scope_label(ns, default_scope),
                url=_scope_url(switch_target, ns, default_scope),
            )
            for ns in known
        ],
        "url_for_scope": lambda path, **params: _scope_url(path, active, default_scope, **params),
    }


async def _resolve_scope(
    store: MnemoStore, requested: str | None
) -> tuple[MnemoStore | None, list[str]]:
    known = await _known_scopes(store)
    if not requested:
        return store, known
    if requested in known:
        return _scoped_store(store, requested), known
    return None, known


def _unknown_scope_response(
    request: Request,
    requested: str,
    known: list[str],
    default_scope: str,
    *,
    partial: bool = False,
) -> HTMLResponse:
    if partial:
        return HTMLResponse(
            '<tr><td colspan="8" role="alert" '
            'class="px-4 py-10 text-center text-amber-700">'
            f"Unknown scope: {escape(requested)}</td></tr>",
            status_code=400,
        )
    return TEMPLATES.TemplateResponse(
        request,
        "error.html",
        {
            "requested_scope": requested,
            **_scope_context(known, default_scope, default_scope, switch_target="/"),
        },
        status_code=400,
    )


async def _facts(store: MnemoStore, q: str):
    # Browsing is not recall: a person's search leaves the recall counters alone.
    return await store.search(q, reinforce=False) if q.strip() else await store.list_current()


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    scope: str | None = None,
    store: MnemoStore = Depends(get_store),
):
    scoped, known = await _resolve_scope(store, scope)
    if scoped is None:
        return _unknown_scope_response(request, scope or "", known, store.namespace)
    facts = await _facts(scoped, q)
    return TEMPLATES.TemplateResponse(
        request,
        "list.html",
        {
            "facts": facts,
            "q": q,
            **_scope_context(known, scoped.namespace, store.namespace, switch_target="/"),
        },
    )


@app.get("/retracted", response_class=HTMLResponse)
async def retracted(
    request: Request,
    scope: str | None = None,
    store: MnemoStore = Depends(get_store),
):
    scoped, known = await _resolve_scope(store, scope)
    if scoped is None:
        return _unknown_scope_response(request, scope or "", known, store.namespace)
    items = await DirectMemory(scoped).retracted()
    return TEMPLATES.TemplateResponse(
        request,
        "retracted.html",
        {
            "items": items,
            **_scope_context(known, scoped.namespace, store.namespace, switch_target="/retracted"),
        },
    )


@app.get("/rows", response_class=HTMLResponse)
async def rows(
    request: Request,
    q: str = "",
    scope: str | None = None,
    store: MnemoStore = Depends(get_store),
):
    scoped, known = await _resolve_scope(store, scope)
    if scoped is None:
        return _unknown_scope_response(request, scope or "", known, store.namespace, partial=True)
    facts = await _facts(scoped, q)
    return TEMPLATES.TemplateResponse(
        request,
        "_rows.html",
        {
            "facts": facts,
            **_scope_context(known, scoped.namespace, store.namespace, switch_target="/"),
        },
    )


async def _fact_response(
    request: Request,
    fact_id: UUID,
    store: MnemoStore,
    *,
    default_scope: str,
    known: list[str],
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
    if store.namespace not in known:
        known = [*known, store.namespace]
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
            **_scope_context(known, store.namespace, default_scope, switch_target="/"),
        },
        status_code=status_code,
    )


@app.get("/fact/{fact_id}", response_class=HTMLResponse)
async def fact_detail(
    request: Request,
    fact_id: UUID,
    scope: str | None = None,
    store: MnemoStore = Depends(get_store),
):
    scoped, known = await _resolve_scope(store, scope)
    if scoped is None:
        return _unknown_scope_response(request, scope or "", known, store.namespace)
    # A memory opens in its own scope, as the edit and restore posts do, so a bare
    # /fact/<id> link to a project memory works.
    fact_ns = await _fact_namespace(store, fact_id)
    target = _scoped_store(store, fact_ns) if fact_ns is not None else scoped
    return await _fact_response(
        request, fact_id, target, default_scope=store.namespace, known=known
    )


@app.post("/fact/{fact_id}/revert/{event_id}")
async def do_revert(
    request: Request,
    fact_id: UUID,
    event_id: UUID,
    scope: str | None = None,
    expected_event_id: UUID = Form(...),
    request_id: UUID = Form(...),
    store: MnemoStore = Depends(get_store),
):
    scoped, known = await _resolve_scope(store, scope)
    if scoped is None:
        return _unknown_scope_response(request, scope or "", known, store.namespace)
    fact_ns = await _fact_namespace(store, fact_id)
    target = _scoped_store(store, fact_ns) if fact_ns is not None else scoped
    try:
        await DirectMemory(target).revert(
            fact_id,
            event_id,
            expected_event_id=expected_event_id,
            request_id=request_id,
            actor="ui",
        )
    except MnemoError as exc:
        return await _refused(request, fact_id, target, store.namespace, known, exc, "restore")
    return RedirectResponse(
        _scope_url(f"/fact/{fact_id}", target.namespace, store.namespace), status_code=303
    )


@app.post("/fact/{fact_id}/edit")
async def do_edit(
    request: Request,
    fact_id: UUID,
    scope: str | None = None,
    value: str = Form(...),
    expected_event_id: UUID = Form(...),
    request_id: UUID = Form(...),
    store: MnemoStore = Depends(get_store),
):
    """A guarded correction: it applies only to the revision the page showed, so an
    agent's write made while the page was open is never silently overwritten."""
    scoped, known = await _resolve_scope(store, scope)
    if scoped is None:
        return _unknown_scope_response(request, scope or "", known, store.namespace)
    fact_ns = await _fact_namespace(store, fact_id)
    target = _scoped_store(store, fact_ns) if fact_ns is not None else scoped
    if not value.strip():
        return await _fact_response(
            request,
            fact_id,
            target,
            default_scope=store.namespace,
            known=known,
            message="Enter a corrected value.",
            status_code=400,
        )
    try:
        await DirectMemory(target).correct(
            fact_id,
            value.strip(),
            expected_event_id=expected_event_id,
            request_id=request_id,
        )
    except MnemoError as exc:
        return await _refused(request, fact_id, target, store.namespace, known, exc, "correction")
    return RedirectResponse(
        _scope_url(f"/fact/{fact_id}", target.namespace, store.namespace), status_code=303
    )


async def _refused(
    request: Request,
    fact_id: UUID,
    store: MnemoStore,
    default_scope: str,
    known: list[str],
    exc: MnemoError,
    action: str,
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
    return await _fact_response(
        request,
        fact_id,
        store,
        default_scope=default_scope,
        known=known,
        message=message,
        status_code=status,
    )


@app.get("/diff", response_class=HTMLResponse)
async def diff_view(
    request: Request,
    a: str = "",
    b: str = "",
    scope: str | None = None,
    store: MnemoStore = Depends(get_store),
):
    scoped, known = await _resolve_scope(store, scope)
    if scoped is None:
        return _unknown_scope_response(request, scope or "", known, store.namespace)
    commits = await scoped.list_commits()
    diff, message = None, None
    if a and b:
        try:
            diff = await scoped.diff(UUID(a), UUID(b))
        except ValueError:  # not a UUID, or not a commit in this scope
            message = "Pick two snapshots from the lists below."
    return TEMPLATES.TemplateResponse(
        request,
        "diff.html",
        {
            "commits": commits,
            "diff": diff,
            "a": a,
            "b": b,
            "message": message,
            **_scope_context(known, scoped.namespace, store.namespace, switch_target="/diff"),
        },
        status_code=400 if message else 200,
    )


@app.post("/commit")
async def do_commit(
    request: Request,
    scope: str | None = None,
    label: str = Form(""),
    store: MnemoStore = Depends(get_store),
):
    scoped, known = await _resolve_scope(store, scope)
    if scoped is None:
        return _unknown_scope_response(request, scope or "", known, store.namespace)
    await scoped.commit(label.strip() or None)
    return RedirectResponse(_scope_url("/diff", scoped.namespace, store.namespace), status_code=303)
