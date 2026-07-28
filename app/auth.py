"""Virtual-key authentication.

Lookup is by SHA-256 hash, so the raw key never needs comparing (and never reaches the
database or the logs). Revocation is checked on every request: keys are never cached.
"""

import hashlib
import secrets
from typing import Annotated

from fastapi import Header
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.db import session_scope
from app.errors import invalid_api_key, server_error
from app.logging import get_logger
from app.models import VirtualKey

logger = get_logger(__name__)

KEY_PREFIX = "lgw_"
KEY_BODY_CHARS = 40


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str, str]:
    """Return (raw key, sha-256 hash, last four characters). The raw key is shown once."""
    body = secrets.token_urlsafe(64).replace("-", "").replace("_", "")[:KEY_BODY_CHARS]
    raw_key = f"{KEY_PREFIX}{body}"
    return raw_key, hash_key(raw_key), raw_key[-4:]


def _parse_bearer(header_value: str | None) -> str | None:
    if not header_value:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def require_key(
    authorization: Annotated[str | None, Header()] = None,
) -> VirtualKey:
    token = _parse_bearer(authorization)
    if token is None:
        logger.warning("auth.denied", extra={"reason": "missing_or_malformed_header"})
        raise invalid_api_key("Missing or malformed Authorization header.")

    try:
        with session_scope() as session:
            key = session.scalar(select(VirtualKey).where(VirtualKey.key_hash == hash_key(token)))
    except SQLAlchemyError as exc:
        logger.error("auth.error", extra={"reason": str(exc)})
        raise server_error() from exc

    if key is None:
        logger.warning("auth.denied", extra={"reason": "unknown_key"})
        raise invalid_api_key()
    if not key.active or key.revoked_at is not None:
        logger.warning("auth.denied", extra={"reason": "revoked_key", "key_name": key.name})
        raise invalid_api_key()
    return key
