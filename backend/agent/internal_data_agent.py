"""Internal data agent — answers user questions about their Leafy Bank data via MongoDB MCP."""

import logging

from langchain.agents import create_agent
from langchain_aws import ChatBedrockConverse

from config import AWS_REGION, CHAT_COMPLETIONS_MODEL_ID
from agent.tools.internal_tools import get_current_user_id

logger = logging.getLogger(__name__)


def create_internal_data_agent(system_prompt: str, mcp_tools: list):
    """Build and return the internal data agent with MongoDB MCP tools.

    Args:
        system_prompt: The agent's system prompt, loaded from encrypted MongoDB.
        mcp_tools: LangChain tools loaded from the MongoDB Atlas MCP server.
    """
    llm = ChatBedrockConverse(
        model=CHAT_COMPLETIONS_MODEL_ID,
        region_name=AWS_REGION,
        temperature=0,
    )

    tools = [get_current_user_id] + mcp_tools

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        name="internal_data_agent",
    )

    logger.info("Internal data agent created successfully")
    return agent
