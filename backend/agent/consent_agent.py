import logging
from pathlib import Path

from langchain.agents import create_agent
from langchain_aws import ChatBedrockConverse
from langgraph.checkpoint.mongodb import MongoDBSaver

from config import (
    AWS_REGION,
    CHAT_COMPLETIONS_MODEL_ID,
    DATABASE_NAME,
    CHECKPOINTS_AIO_COLLECTION,
    CHECKPOINTS_WRITES_AIO_COLLECTION,
)
from agent.db.mdb import MongoDBConnector
from agent.tools.consent_tools import (
    list_institutions,
    get_default_permissions,
    create_consent,
    get_consent,
    list_user_consents,
    request_bank_login,
    approve_consent,
    revoke_consent,
)

logger = logging.getLogger(__name__)

# Load system prompt from markdown file
PROMPT_PATH = Path(__file__).parent / "prompts" / "consent.md"
SYSTEM_PROMPT = PROMPT_PATH.read_text()

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
]

# Shared MongoDB connector — single client for the app
db = MongoDBConnector()


def get_checkpointer() -> MongoDBSaver:
    """Create and return a MongoDBSaver using the shared MongoDB client."""
    return MongoDBSaver(
        client=db.client,
        db_name=DATABASE_NAME,
        checkpoint_collection_name=CHECKPOINTS_AIO_COLLECTION,
        writes_collection_name=CHECKPOINTS_WRITES_AIO_COLLECTION,
    )


def create_consent_agent(checkpointer: MongoDBSaver):
    """Build and return the compiled consent agent graph."""
    llm = ChatBedrockConverse(
        model=CHAT_COMPLETIONS_MODEL_ID,
        region_name=AWS_REGION,
        temperature=0,
    )

    agent = create_agent(
        model=llm,
        tools=TOOLS,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
    )

    logger.info("Consent agent created successfully")
    return agent
