"""Shared authentication helper for agent tools."""

import time

from http_client import http_client

_token_cache: dict[str, tuple[str, float]] = {}
_TOKEN_TTL = 300  # 5 minutes

async def get_bearer_token(user_id: str) -> str:
    """Get bearer token for a user, with in-memory caching."""
    now = time.monotonic()
    cached = _token_cache.get(user_id)
    if cached and (now - cached[1]) < _TOKEN_TTL:
        return cached[0]

    response = await http_client.get(
        "/openfinance/public/get-authorization",
        params={"user_identifier": user_id},
    )
    response.raise_for_status()
    token = response.json()["BearerToken"]
    _token_cache[user_id] = (token, now)
    return token
