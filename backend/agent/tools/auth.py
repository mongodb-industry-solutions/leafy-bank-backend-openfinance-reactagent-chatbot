"""Shared authentication helper for agent tools."""

from http_client import http_client


async def get_bearer_token(user_id: str) -> str:
    """Get bearer token for a user from the Open Finance backend."""
    response = await http_client.get(
        "/openfinance/public/get-authorization",
        params={"user_identifier": user_id},
    )
    response.raise_for_status()
    return response.json()["BearerToken"]
