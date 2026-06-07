# Mnemo

**Git for agent memory** — see, diff, blame, and roll back what your AI remembers.

An append-only, inspectable, reversible memory layer for LLM agents:
`add` / `search` / `blame` / `revert` / `diff` / `log`, on plain Postgres,
exposed via an MCP server + Python SDK.

> 🚧 Early development. Built milestone by milestone (see `PROJECT_SPEC.md` §12).
> The full README — quickstart, rollback GIF, comparison table — lands in **M8**.

## Quickstart (so far)

```bash
make install   # create venv + install (uses uv)
make up        # start Postgres + pgvector
make migrate   # apply migrations (none yet — schema lands in M1)
make test      # run the suite
```

Configuration lives in [`mnemo/config.py`](mnemo/config.py); copy `.env.example` → `.env`
to override. Default backend is **local Ollama** (free, offline); OpenAI is a config switch.

See [`PROJECT_SPEC.md`](PROJECT_SPEC.md) for the design and [`CLAUDE.md`](CLAUDE.md)
for how the project is built.
