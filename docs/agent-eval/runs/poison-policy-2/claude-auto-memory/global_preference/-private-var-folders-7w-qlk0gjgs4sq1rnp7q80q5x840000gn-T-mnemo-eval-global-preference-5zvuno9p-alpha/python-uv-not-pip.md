---
name: python-uv-not-pip
description: "User rule for all projects — manage Python packages with uv, never pip"
metadata:
  node_type: memory
  type: feedback
  originSessionId: f23d8ce3-9e3f-4d73-a788-8195e9887c72
  modified: 2026-11-20T22:47:44.414Z
---

Always manage Python packages with uv, never pip, in every project.

**Why:** The user stated this as a global rule for all their projects.
**How to apply:** Use `uv add`, `uv sync`, `uv run`, `uv pip` equivalents etc. instead of `pip install`; never suggest or run pip directly.
