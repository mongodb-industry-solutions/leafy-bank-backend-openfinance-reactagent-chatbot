"""Supervisor node — routes between consent_agent, portability_agent, and FINISH."""

import json
import logging
from typing import Callable, Literal, Optional

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from config import AWS_REGION, SUPERVISOR_MODEL_ID
from state import ConsentInfo

logger = logging.getLogger(__name__)

# Single LLM instance reused across all supervisor calls
_supervisor_llm = ChatBedrockConverse(
    model=SUPERVISOR_MODEL_ID,
    region_name=AWS_REGION,
    temperature=0,
)


class RouterDecision(BaseModel):
    """Supervisor routing decision."""

    next: Literal["consent_agent", "portability_agent", "internal_data_agent", "FINISH"] = Field(
        description="Which agent to route to, or FINISH to end the turn."
    )
    response: str = Field(
        default="",
        description="Message to send to the user. Required when next is FINISH.",
    )


_supervisor_llm_structured = _supervisor_llm.with_structured_output(RouterDecision)


def _extract_new_consent_from_messages(
    messages: list, existing_ids: set
) -> Optional[ConsentInfo]:
    """Scan recent tool messages for a NEWLY approved consent not already tracked.

    Searches backward (most recent first), limited to the last 20 tool messages.
    If the approval message doesn't include purpose, continues scanning for the
    matching create_consent message that does.
    """
    tool_count = 0
    consent_id = None
    purpose = None
    institution = None

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
                cid = data.get("consent_id")
                if cid and cid not in existing_ids:
                    consent_id = cid
                    purpose = data.get("purpose")
                    institution = data.get("source_institution", "")
                    if consent_id and purpose:
                        return ConsentInfo(
                            consent_id=consent_id,
                            purpose=purpose,
                            institution=institution,
                        )
            elif (
                consent_id
                and data.get("consent_id") == consent_id
                and data.get("purpose")
            ):
                # Found the create_consent message with purpose for same consent
                return ConsentInfo(
                    consent_id=consent_id,
                    purpose=data["purpose"],
                    institution=institution or data.get("source_institution", ""),
                )
        except (json.JSONDecodeError, TypeError):
            continue

    if consent_id:
        return ConsentInfo(
            consent_id=consent_id,
            purpose=purpose,
            institution=institution or "",
        )
    return None


def create_supervisor_node(system_prompt: str) -> Callable:
    """Create a supervisor node function with the given system prompt.

    Args:
        system_prompt: The supervisor's system prompt, loaded from encrypted MongoDB.

    Returns:
        An async function compatible with LangGraph's StateGraph.add_node().
    """

    async def supervisor_node(state: dict) -> dict:
        """Route the conversation to the appropriate agent or respond directly."""
        messages = state.get("messages", [])
        active_consents: list[ConsentInfo] = list(state.get("active_consents") or [])
        existing_ids = {c["consent_id"] for c in active_consents}

        # Check if a consent was just approved (not yet tracked in state)
        newly_approved = False
        new_consent = _extract_new_consent_from_messages(messages, existing_ids)
        if new_consent:
            active_consents.append(new_consent)
            newly_approved = True
            logger.info(
                f"Supervisor detected approved consent: {new_consent['consent_id']}, "
                f"purpose: {new_consent['purpose']}, institution: {new_consent['institution']}"
            )

        # Derive latest consent info for handoff messages
        latest_consent = active_consents[-1] if active_consents else None
        latest_purpose = latest_consent["purpose"] if latest_consent else None

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
                    "active_consents": active_consents,
                }

            # Fallback: consent agent didn't respond (edge case) — generate
            # a confirmation message so the user always sees one.
            if "LOAN_PORTABILITY" in (latest_purpose or "").upper():
                analysis_desc = (
                    "I can now analyze your external bank data to evaluate loan "
                    "portability — this checks your spending patterns, credit score, "
                    "and finds better Leafy Bank rates."
                )
            elif (latest_purpose or "").upper() == "FINANCIAL_ADVICE":
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

            bank_count = len(active_consents)
            bank_summary = ""
            if bank_count > 1:
                banks = [c["institution"] for c in active_consents if c["institution"]]
                bank_summary = (
                    f" You now have {bank_count} bank connections active: "
                    f"{', '.join(banks)}."
                )

            confirmation_msg = (
                f"Your consent is now active! Your data from the external bank "
                f"is securely connected.{bank_summary}\n\n"
                f"{analysis_desc}\n\n"
                f"Would you like me to proceed with the analysis?"
            )
            logger.info("Supervisor: consent just approved, pausing for user confirmation (fallback)")
            return {
                "next": "FINISH",
                "active_consents": active_consents,
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
                "active_consents": active_consents,
            }

        # Build context for LLM
        context_parts = []
        if active_consents:
            context_parts.append(f"Active consents ({len(active_consents)}):")
            for c in active_consents:
                context_parts.append(
                    f"  - {c['consent_id']} | {c['institution']} | purpose: {c['purpose']}"
                )
        else:
            context_parts.append("No active consents.")

        context = "\n".join(context_parts)
        system_msg = SystemMessage(
            content=f"{system_prompt}\n\n## Current State\n{context}"
        )

        decision: RouterDecision = await _supervisor_llm_structured.ainvoke(
            [system_msg] + messages
        )

        logger.info(f"Supervisor decision: next={decision.next}")

        result = {
            "next": decision.next,
            "active_consents": active_consents,
        }

        # When FINISH, add the supervisor's response as an AI message
        if decision.next == "FINISH" and decision.response:
            result["messages"] = [AIMessage(content=decision.response)]
        # When routing to a sub-agent with active consents, inject a handoff
        # message so the agent has consent info clearly in recent history.
        # Uses HumanMessage — Claude 4.6 rejects conversations ending with an
        # assistant message (prefill no longer supported). The handoff is context
        # *for* the sub-agent, not a response *from* the assistant.
        elif decision.next == "portability_agent" and active_consents:
            consents_summary = json.dumps(
                [{"consent_id": c["consent_id"], "institution": c["institution"],
                  "purpose": c["purpose"]} for c in active_consents]
            )
            handoff = (
                f"[Supervisor handoff] Routing to portability agent. "
                f"Active consents: {consents_summary}"
            )
            msgs = []
            if decision.response:
                msgs.append(AIMessage(content=decision.response))
            msgs.append(HumanMessage(content=handoff))
            result["messages"] = msgs

        return result

    return supervisor_node
