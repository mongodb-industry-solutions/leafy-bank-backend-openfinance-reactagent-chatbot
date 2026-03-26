"""Consent agent — guides users through Open Finance consent creation and management."""

import logging

from langchain.agents import create_agent
from langchain_aws import ChatBedrockConverse

from config import AWS_REGION, CHAT_COMPLETIONS_MODEL_ID
from agent.tools.consent_tools import (
    list_institutions,
    get_default_permissions,
    create_consent,
    get_consent,
    list_user_consents,
    request_bank_login,
    approve_consent,
    revoke_consent,
    verify_consent_data,
)

logger = logging.getLogger(__name__)

# All consent tools
TOOLS = [
    list_institutions,
    get_default_permissions,
    create_consent,
    get_consent,
    list_user_consents,
    request_bank_login,
    approve_consent,
    revoke_consent,
    verify_consent_data,
]


def create_consent_agent(system_prompt: str):
    """Build and return the consent agent graph (no checkpointer).

    Args:
        system_prompt: The agent's system prompt, loaded from encrypted MongoDB.
    """
    llm = ChatBedrockConverse(
        model=CHAT_COMPLETIONS_MODEL_ID,
        region_name=AWS_REGION,
        temperature=0,
    )

    agent = create_agent(
        model=llm,
        tools=TOOLS,
        system_prompt=system_prompt,
        name="consent_agent",
    )

    logger.info("Consent agent created successfully")
    return agent
