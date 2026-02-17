"""Supervisor node — routes between consent_agent, analysis_agent, and FINISH."""

import json
import logging
from pathlib import Path
from typing import Literal, Optional

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, SystemMessage
from pydantic import BaseModel, Field

from config import AWS_REGION, CHAT_COMPLETIONS_MODEL_ID

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "supervisor.md"
SYSTEM_PROMPT = PROMPT_PATH.read_text()

# Single LLM instance reused across all supervisor calls
_supervisor_llm = ChatBedrockConverse(
    model=CHAT_COMPLETIONS_MODEL_ID,
    region_name=AWS_REGION,
    temperature=0,
)


class RouterDecision(BaseModel):
    """Supervisor routing decision."""

    next: Literal["consent_agent", "analysis_agent", "FINISH"] = Field(
        description="Which agent to route to, or FINISH to end the turn."
    )
    response: str = Field(
        default="",
        description="Message to send to the user. Required when next is FINISH.",
    )


_supervisor_llm_structured = _supervisor_llm.with_structured_output(RouterDecision)


def _extract_consent_info_from_messages(messages: list) -> tuple[Optional[str], Optional[str]]:
    """Scan recent tool messages for an approved consent, return (consent_id, purpose)."""
    for msg in reversed(messages):
        if not hasattr(msg, "type") or msg.type != "tool":
            continue
        try:
            data = json.loads(msg.content)
            if isinstance(data, dict) and data.get("status") == "AUTHORISED":
                return data.get("consent_id"), data.get("purpose")
        except (json.JSONDecodeError, TypeError):
            continue
    return None, None


async def supervisor_node(state: dict) -> dict:
    """Route the conversation to the appropriate agent or respond directly."""
    messages = state.get("messages", [])
    active_consent_id = state.get("active_consent_id")
    active_consent_purpose = state.get("active_consent_purpose")

    # Check if a consent was just approved (not yet tracked in state)
    newly_approved = False
    if not active_consent_id:
        found_id, found_purpose = _extract_consent_info_from_messages(messages)
        if found_id:
            active_consent_id = found_id
            active_consent_purpose = found_purpose
            newly_approved = True
            logger.info(
                f"Supervisor detected approved consent: {found_id}, purpose: {found_purpose}"
            )

    # Quick exit: if the last message is from a sub-agent (AI) and no consent
    # was just approved, route to FINISH so the user sees the response.
    # This prevents looping (supervisor → agent → supervisor → agent ...).
    last_msg = messages[-1] if messages else None
    if (
        last_msg
        and hasattr(last_msg, "type")
        and last_msg.type == "ai"
        and last_msg.content
        and not newly_approved
    ):
        logger.info("Supervisor: sub-agent responded, returning to user")
        return {
            "next": "FINISH",
            "active_consent_id": active_consent_id,
            "active_consent_purpose": active_consent_purpose,
        }

    # Build context for LLM
    context_parts = []
    if active_consent_id:
        context_parts.append(f"Active consent: {active_consent_id}")
        context_parts.append(f"Consent purpose: {active_consent_purpose}")
    else:
        context_parts.append("No active consent.")

    context = "\n".join(context_parts)
    system_msg = SystemMessage(
        content=f"{SYSTEM_PROMPT}\n\n## Current State\n{context}"
    )

    decision: RouterDecision = await _supervisor_llm_structured.ainvoke(
        [system_msg] + messages
    )

    logger.info(f"Supervisor decision: next={decision.next}")

    result = {
        "next": decision.next,
        "active_consent_id": active_consent_id,
        "active_consent_purpose": active_consent_purpose,
    }

    # When FINISH, add the supervisor's response as an AI message
    if decision.next == "FINISH" and decision.response:
        result["messages"] = [AIMessage(content=decision.response)]

    return result
