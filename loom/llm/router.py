"""LLM router built on LiteLLM with explicit provider keys and timeouts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from loom import constants
from loom.errors import LLMError

if TYPE_CHECKING:
    from loom.config import LoomConfig

logger = structlog.get_logger("loom.llm.router")


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model_used: str
    usage: dict | None = None


class LLMRouter:
    def __init__(self, config: LoomConfig):
        self.config = config

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = constants.DEFAULT_LLM_MAX_TOKENS,
        temperature: float = constants.DEFAULT_LLM_TEMPERATURE,
        timeout_seconds: int | None = None,
    ) -> LLMResponse:
        if self.config.llm_provider == "skip" or not self.config.llm_api_key:
            raise LLMError("LLM provider is disabled or missing an API key")

        model = self.config.llm_model_id
        timeout = timeout_seconds or self.config.llm_timeout_seconds

        logger.debug(
            "llm_complete",
            model=model,
            system_chars=len(system),
            user_chars=len(user),
        )

        try:
            import litellm

            litellm.suppress_debug_info = True

            async with asyncio.timeout(timeout):
                resp = await litellm.acompletion(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature,
                    api_key=self.config.llm_api_key,
                    timeout=timeout,
                )
        except TimeoutError as exc:
            logger.warning("llm_timeout", model=model, timeout=timeout)
            raise LLMError(
                "The LLM request timed out. Try again or set "
                "LOOM_LLM_MODEL_OVERRIDE to a faster model."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - convert to typed error
            logger.warning(
                "llm_failed", model=model, error_type=type(exc).__name__
            )
            raise LLMError(
                "The LLM provider returned an error. If the model is rejected, "
                "set LOOM_LLM_MODEL_OVERRIDE to a supported model."
            ) from exc

        try:
            text = resp["choices"][0]["message"]["content"] or ""
            usage = dict(resp["usage"]) if resp.get("usage") else None
            model_used = resp.get("model", model)
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("The LLM returned an unexpected response shape.") from exc

        return LLMResponse(text=text, model_used=model_used, usage=usage)
