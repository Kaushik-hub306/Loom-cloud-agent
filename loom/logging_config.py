"""Centralized structlog configuration.

``configure_logging`` is idempotent across the process. It chooses JSON output
in production and human-readable console output otherwise.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from loom.config import LoomConfig

_CONFIGURED = False


def configure_logging(config: LoomConfig) -> None:
    """Configure structlog once for the process.

    Calling this repeatedly (for example in tests) is a no-op after the first
    successful configuration.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = (config.log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=level,
    )

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if config.env == "production":
        renderer: structlog.typing.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    _CONFIGURED = True


def reset_logging_for_tests() -> None:
    """Reset the one-time guard. Intended only for test isolation."""
    global _CONFIGURED
    _CONFIGURED = False
    structlog.reset_defaults()


def get_logger(name: str) -> structlog.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)
