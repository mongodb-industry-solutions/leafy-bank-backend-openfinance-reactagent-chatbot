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
    """Scan recent tool messages for an approved consent, return (consent_id, purpose).

    Searches backward (most recent first), limited to the last 20 tool messages.
    If the approval message doesn't include purpose, continues scanning for the
    matching create_consent message that does.
    """
    tool_count = 0
    consent_id = None
    purpose = None

    for msg in reversed(messages):
        if not hasattr(msg, "type") or msg.type != "tool":
            continue

        tool_count += 1
        if tool_count > 20:
            break

        try:
            data = json.loads(msg.content)
            if not isinstance(data, dict):
                continue

            status = (data.get("status") or "").upper()

            if status == "AUTHORISED" and not consent_id:
                consent_id = data.get("consent_id")
                purpose = data.get("purpose")
                if consent_id and purpose:
                    return consent_id, purpose
            elif (
                consent_id
                and data.get("consent_id") == consent_id
                and data.get("purpose")
            ):
                # Found the create_consent message with purpose for same consent
                return consent_id, data["purpose"]
        except (json.JSONDecodeError, TypeError):
            continue

    return consent_id, purpose


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

    # Hard guard: when consent was just approved, always pause and confirm
    # with the user before routing to analysis. This is deterministic — the
    # LLM cannot skip this step.
    if newly_approved:
        # If the consent agent already responded (e.g. with verification
        # summary from verify_consent_data), preserve its message — just
        # set state and FINISH. Only generate a fallback confirmation if
        # the consent agent didn't produce a response.
        last_msg = messages[-1] if messages else None
        if (
            last_msg
            and hasattr(last_msg, "type")
            and last_msg.type == "ai"
            and last_msg.content
        ):
            logger.info(
                "Supervisor: consent just approved, consent agent already "
                "responded — preserving its message"
            )
            return {
                "next": "FINISH",
                "active_consent_id": active_consent_id,
                "active_consent_purpose": active_consent_purpose,
            }

        # Fallback: consent agent didn't respond (edge case) — generate
        # a confirmation message so the user always sees one.
        if "LOAN_PORTABILITY" in (active_consent_purpose or "").upper():
            analysis_desc = (
                "I can now analyze your external bank data to evaluate loan "
                "portability — this checks your spending patterns, credit score, "
                "and finds better Leafy Bank rates."
            )
        elif (active_consent_purpose or "").upper() == "FINANCIAL_ADVICE":
            analysis_desc = (
                "I can now analyze your spending across all your accounts, "
                "compare against best practices, and provide a financial "
                "overview with personalized recommendations."
            )
        else:
            analysis_desc = (
                "I can now analyze your financial data from the connected "
                "bank and provide insights."
            )

        confirmation_msg = (
            f"Your consent is now active! Your data from the external bank "
            f"is securely connected.\n\n"
            f"{analysis_desc}\n\n"
            f"Would you like me to proceed with the analysis?"
        )
        logger.info("Supervisor: consent just approved, pausing for user confirmation (fallback)")
        return {
            "next": "FINISH",
            "active_consent_id": active_consent_id,
            "active_consent_purpose": active_consent_purpose,
            "messages": [AIMessage(content=confirmation_msg)],
        }

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
    # When routing to analysis_agent with an active consent, inject a handoff
    # message so the analysis agent has consent_id clearly in recent history
    elif decision.next == "analysis_agent" and active_consent_id:
        handoff = (
            f"Routing to analysis agent. "
            f"Active consent ID: {active_consent_id} "
            f"| Purpose: {active_consent_purpose}"
        )
        if decision.response:
            handoff = f"{decision.response}\n\n[{handoff}]"
        result["messages"] = [AIMessage(content=handoff)]

    return result
