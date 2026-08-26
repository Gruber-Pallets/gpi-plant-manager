"""Safe, shared presentation helpers for local feedback content."""

from __future__ import annotations

from urllib.parse import urlparse


def safe_page_url(value: str | None) -> str | None:
    """Keep only the page URL forms safe to render as a link."""
    value = (value or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https"):
        return value
    if not parsed.scheme and value.startswith("/") and not value.startswith("//"):
        return value
    return None
