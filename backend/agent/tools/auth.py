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


_user_cache: dict[str, tuple[dict, float]] = {}
_USER_CACHE_TTL = 300  # 5 minutes

async def get_user_profile(user_id: str) -> dict:
    """Get Leafy Bank user profile, with in-memory caching.

    Returns the full user dict (including _id, UserName, etc.).
    Cache keyed by user_id, expires after 5 minutes.
    """
    now = time.monotonic()
    cached = _user_cache.get(user_id)
    if cached and (now - cached[1]) < _USER_CACHE_TTL:
        return cached[0]

    response = await http_client.post(
        "/leafybank/users/secure/find-user",
        json={"user_identifier": user_id},
    )
    response.raise_for_status()
    user = response.json()["user"]
    _user_cache[user_id] = (user, now)
    return user
