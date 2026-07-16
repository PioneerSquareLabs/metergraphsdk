import hashlib
import hmac
import os

from fastapi import HTTPException, Request


def _tokens() -> list[str]:
    raw = os.environ.get("MG_TOKENS", "")
    return [token.strip() for token in raw.split(",") if token.strip()]


def require_token(request: Request) -> None:
    header = request.headers.get("authorization") or ""
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented.strip():
        raise HTTPException(401, "missing bearer token")
    presented_digest = hashlib.sha256(presented.strip().encode()).digest()
    for token in _tokens():
        expected = hashlib.sha256(token.encode()).digest()
        if hmac.compare_digest(presented_digest, expected):
            return
    raise HTTPException(401, "invalid token")
