"""Shared httpx async client for Open Finance API calls.

Single instance used by all tool modules. Closed in the FastAPI lifespan.
"""

import httpx

from config import OPEN_FINANCE_API_BASE

http_client = httpx.AsyncClient(
    base_url=OPEN_FINANCE_API_BASE,
    timeout=30.0,
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
)
