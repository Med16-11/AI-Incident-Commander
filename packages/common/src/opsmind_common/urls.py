"""Validation for operator-configured outbound service URLs."""

from urllib.parse import urlsplit


def validate_outbound_base_url(
    value: str,
    *,
    name: str,
    allow_insecure_http: bool = False,
) -> str:
    """Reject malformed URLs, embedded credentials, and unapproved cleartext transport.

    Hosts remain operator-configured; callers must also restrict outbound network access at
    deployment time. HTTP is permitted only when the caller explicitly enables local mode.
    """
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing port also validates malformed port values.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid HTTP service URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be a base URL without credentials, query, or fragment")
    if parsed.scheme != "https" and not allow_insecure_http:
        raise ValueError(f"{name} must use HTTPS outside local development")
    return value.rstrip("/")
