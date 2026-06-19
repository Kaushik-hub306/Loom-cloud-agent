"""HTTP client the Slack worker uses to talk to the FastAPI service.

Slack never touches the database directly; all reads/writes go through this
client over HTTP. Public routes attach the API key; internal routes attach the
internal token.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from loom.errors import SlackAPIError

if TYPE_CHECKING:
    from loom.config import LoomConfig

logger = structlog.get_logger("loom.slack.client")

_RETRYABLE_STATUS = {429, 502, 503, 504}
_MAX_RETRIES = 4
_BASE_BACKOFF_SECONDS = 0.5


class LoomAPIClient:
    def __init__(self, config: LoomConfig):
        self.config = config
        self.base_url = config.api_base_url.rstrip("/")
        self.api_key = config.api_key
        self.internal_token = config.internal_api_token
        self.timeout = config.http_timeout_seconds
        self._max_backoff = config.slack_reconnect_backoff_max_seconds
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    def _public_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-Loom-Api-Key"] = self.api_key
        return headers

    def _internal_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.internal_token:
            headers["X-Loom-Internal-Token"] = self.internal_token
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json: Any | None = None,
        params: dict | None = None,
    ) -> dict:
        client = self._get_client()
        backoff = _BASE_BACKOFF_SECONDS
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = await client.request(
                    method, path, headers=headers, json=json, params=params
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "api_request_transport_error",
                    path=path,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(min(backoff, self._max_backoff))
                backoff *= 2
                continue

            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES - 1:
                delay = self._retry_delay(resp, backoff)
                logger.warning(
                    "api_request_retry",
                    path=path,
                    status=resp.status_code,
                    attempt=attempt,
                    delay=round(delay, 2),
                )
                await asyncio.sleep(delay)
                backoff *= 2
                continue

            if resp.status_code >= 400:
                raise SlackAPIError(
                    f"API call to {path} failed with status {resp.status_code}.",
                    details={"status": resp.status_code, "path": path},
                )

            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError as exc:
                raise SlackAPIError(
                    f"API call to {path} returned invalid JSON.",
                    details={"path": path},
                ) from exc

        raise SlackAPIError(
            f"API call to {path} failed after retries.",
            details={"path": path, "error_type": type(last_exc).__name__ if last_exc else None},
        )

    def _retry_delay(self, resp: httpx.Response, backoff: float) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), self._max_backoff)
            except ValueError:
                logger.debug("invalid_retry_after_header")
        return min(backoff, self._max_backoff)

    async def health(self) -> dict:
        return await self._request("GET", "/health", headers=self._public_headers())

    async def ask(self, payload: dict) -> dict:
        return await self._request(
            "POST", "/ask", headers=self._public_headers(), json=payload
        )

    async def teach(self, payload: dict) -> dict:
        return await self._request(
            "POST", "/teach", headers=self._public_headers(), json=payload
        )

    async def recall(self, payload: dict) -> dict:
        return await self._request(
            "POST", "/recall", headers=self._public_headers(), json=payload
        )

    async def stats(self) -> dict:
        return await self._request("GET", "/stats", headers=self._public_headers())

    async def save_conversation_blob(self, payload: dict) -> dict:
        return await self._request(
            "POST",
            "/internal/conversation_blob",
            headers=self._internal_headers(),
            json=payload,
        )

    async def save_context_summary(self, payload: dict) -> dict:
        return await self._request(
            "POST",
            "/internal/context_summary",
            headers=self._internal_headers(),
            json=payload,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
