"""Header-based authentication dependencies.

Public non-health routes use :func:`require_api_key`; internal Slack-write
routes use :func:`require_internal_token`. Secrets are compared with
``hmac.compare_digest`` and never logged. When a secret is unset in a
non-production environment we allow the request and warn exactly once so local
development stays frictionless while production stays locked down.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

import structlog
from fastapi import Depends, Request

from loom.api.deps import get_config
from loom.errors import APIAuthError

if TYPE_CHECKING:
    from loom.config import LoomConfig

logger = structlog.get_logger("loom.api.auth")

API_KEY_HEADER = "X-Loom-Api-Key"
INTERNAL_TOKEN_HEADER = "X-Loom-Internal-Token"  # noqa: S105 - HTTP header name, not a secret

_warned_api_key_unset = False
_warned_internal_token_unset = False


def _warn_once_api_key() -> None:
    global _warned_api_key_unset
    if not _warned_api_key_unset:
        _warned_api_key_unset = True
        logger.warning(
            "api_key_unset_allowing_unauthenticated",
            header=API_KEY_HEADER,
        )


def _warn_once_internal_token() -> None:
    global _warned_internal_token_unset
    if not _warned_internal_token_unset:
        _warned_internal_token_unset = True
        logger.warning(
            "internal_token_unset_allowing_unauthenticated",
            header=INTERNAL_TOKEN_HEADER,
        )


async def require_api_key(
    request: Request, config: LoomConfig = Depends(get_config)
) -> None:
    if not config.api_key:
        # Production startup already fails when this is missing; in dev we allow.
        _warn_once_api_key()
        return
    provided = request.headers.get(API_KEY_HEADER) or ""
    if not hmac.compare_digest(provided, config.api_key):
        raise APIAuthError("Missing or invalid API key.")


async def require_internal_token(
    request: Request, config: LoomConfig = Depends(get_config)
) -> None:
    if not config.internal_api_token:
        _warn_once_internal_token()
        return
    provided = request.headers.get(INTERNAL_TOKEN_HEADER) or ""
    if not hmac.compare_digest(provided, config.internal_api_token):
        raise APIAuthError("Missing or invalid internal token.")
