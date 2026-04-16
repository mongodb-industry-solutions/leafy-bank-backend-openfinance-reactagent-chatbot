"""Portability agent — evaluates financial data for loan portability and financial advice."""

import logging

from langchain.agents import create_agent
from langchain_aws import ChatBedrockConverse

from config import BEDROCK_CLIENT, CHAT_COMPLETIONS_MODEL_ID
from agent.tools.analysis_tools import (
    fetch_credit_score,
    evaluate_portability_offer,
    fetch_internal_accounts,
    calculate_financial_position,
    fetch_customer_identification,
    find_user,
    analyze_spending,
)

logger = logging.getLogger(__name__)

TOOLS = [
    find_user,
    analyze_spending,
    fetch_customer_identification,
    fetch_credit_score,
    evaluate_portability_offer,
    fetch_internal_accounts,
    calculate_financial_position,
]


def create_portability_agent(system_prompt: str):
    """Build and return the portability agent graph (no checkpointer).

    Args:
        system_prompt: The agent's system prompt, loaded from encrypted MongoDB.
    """
    llm = ChatBedrockConverse(
        client=BEDROCK_CLIENT,
        model=CHAT_COMPLETIONS_MODEL_ID,
        temperature=0,
        max_tokens=2048,
    )

    agent = create_agent(
        model=llm,
        tools=TOOLS,
        system_prompt=system_prompt,
        name="portability_agent",
    )

    logger.info("Portability agent created successfully")
    return agent
