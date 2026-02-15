"""Analysis agent — evaluates financial data for loan portability and financial advice."""

import logging
from pathlib import Path

from langchain.agents import create_agent
from langchain_aws import ChatBedrockConverse

from config import AWS_REGION, CHAT_COMPLETIONS_MODEL_ID
from agent.tools.analysis_tools import (
    fetch_external_data,
    fetch_spending_transactions,
    get_spending_best_practices,
    fetch_credit_score,
    get_underwriting_rules,
    find_matching_products,
    fetch_internal_accounts,
    calculate_total_balance,
    calculate_total_debt,
    fetch_customer_identification,
    find_user,
)

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "analysis.md"
SYSTEM_PROMPT = PROMPT_PATH.read_text()

TOOLS = [
    fetch_external_data,
    fetch_spending_transactions,
    get_spending_best_practices,
    fetch_credit_score,
    get_underwriting_rules,
    find_matching_products,
    fetch_internal_accounts,
    calculate_total_balance,
    calculate_total_debt,
    fetch_customer_identification,
    find_user,
]


def create_analysis_agent():
    """Build and return the analysis agent graph (no checkpointer)."""
    llm = ChatBedrockConverse(
        model=CHAT_COMPLETIONS_MODEL_ID,
        region_name=AWS_REGION,
        temperature=0,
    )

    agent = create_agent(
        model=llm,
        tools=TOOLS,
        system_prompt=SYSTEM_PROMPT,
        name="analysis_agent",
    )

    logger.info("Analysis agent created successfully")
    return agent
