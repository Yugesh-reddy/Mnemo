---
name: python-uv-not-pip
description: "User rule for all projects - manage Python packages with uv, never pip"
metadata:
  node_type: memory
  type: feedback
  originSessionId: d55a57cb-62fc-408f-ba20-9fcaba2bcfbb
  modified: 2026-11-19T18:33:16.497Z
---

Always manage Python packages with uv (e.g. `uv add`, `uv pip install`, `uv sync`, `uv run`), never plain pip.

**Why:** The user's stated rule for all of their projects.
**How to apply:** Any time a Python dependency needs installing, adding, or removing, in any project, use uv commands.
