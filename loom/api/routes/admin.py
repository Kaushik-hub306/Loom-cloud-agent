"""Admin router (intentionally minimal).

Design choice: the public ``/export`` and ``/import`` routes are implemented in
``memory.py`` (alongside the other public memory endpoints) rather than here.
This keeps every public, API-key-protected memory operation in one module and
matches the Phase 4 deliverable list for ``memory.py``. This module exists to
satisfy the repository structure and as a home for any future admin-only
operations; it currently exposes no routes.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["admin"])
