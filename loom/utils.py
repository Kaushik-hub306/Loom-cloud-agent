"""Reusable, dependency-free helpers."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")


def redact_secret(value: str | None, *, visible_prefix: int = 6) -> str:
    """Return a redacted form of a secret safe for logs.

    Shows at most ``visible_prefix`` leading characters, then a fixed mask.
    Never returns the full secret. ``None``/empty becomes ``"<unset>"``.
    """
    if not value:
        return "<unset>"
    prefix = value[:visible_prefix]
    return f"{prefix}...<redacted len={len(value)}>"


def slugify(value: str, *, max_chars: int = 80) -> str:
    """Slugify text per Loom rules.

    1. Lowercase.
    2. Strip leading/trailing whitespace.
    3. Replace non-alphanumeric runs with ``-``.
    4. Collapse repeated dashes.
    5. Trim dashes.
    6. Limit to ``max_chars``.
    7. If empty, return first 12 chars of SHA-256 of the original text.
    """
    original = value
    lowered = value.lower().strip()
    replaced = _NON_ALNUM.sub("-", lowered)
    collapsed = re.sub(r"-+", "-", replaced)
    trimmed = collapsed.strip("-")
    limited = trimmed[:max_chars].strip("-")
    if not limited:
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
        return digest[:12]
    return limited


def md5_short(value: str, *, length: int = 16) -> str:
    """Return the first ``length`` hex chars of the MD5 of ``value``."""
    digest = hashlib.md5(value.encode("utf-8")).hexdigest()  # noqa: S324 - id only, not security
    return digest[:length]


def utc_now() -> datetime:
    """Return a timezone-aware UTC ``datetime``."""
    return datetime.now(UTC)


def make_memory_id(domain: str, rule_type: str, rule: str) -> str:
    """Generate the deterministic memory ID used for duplicate detection."""
    return (
        f"{slugify(domain, max_chars=50)}::"
        f"{slugify(rule_type, max_chars=50)}::"
        f"{slugify(rule, max_chars=80)}"
    )


def normalize_whitespace(value: str) -> str:
    """Collapse internal whitespace runs to single spaces and strip ends."""
    return _WHITESPACE.sub(" ", value).strip()
