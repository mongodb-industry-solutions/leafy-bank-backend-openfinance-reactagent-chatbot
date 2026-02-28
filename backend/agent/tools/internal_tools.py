"""Tools for the internal data agent — provides user context for MongoDB queries."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool


@tool
async def get_current_user_id(config: RunnableConfig) -> str:
    """Get the current authenticated user's identifier.

    Always call this tool FIRST before running any MongoDB queries,
    so you can filter data to only this user's records.
    """
    return config["configurable"]["user_id"]
