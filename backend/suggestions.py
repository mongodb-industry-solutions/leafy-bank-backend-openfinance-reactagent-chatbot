"""Contextual reply suggestion generator.

After each assistant response, generates 2-3 short follow-up suggestions
using a lightweight Haiku model. Suggestions appear as clickable chips
in the frontend, guiding users through the demo flow.
"""

import logging
from typing import Optional

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from config import AWS_REGION, SUGGESTIONS_MODEL_ID

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You generate 2-3 short reply suggestions that DIRECTLY ANSWER the assistant's last message.

CRITICAL: Read the assistant's last message carefully. If it asks a question, your suggestions must be plausible ANSWERS to that question — not generic actions.

Examples:
- Assistant asks "Which bank holds your loan?" → suggest bank names mentioned in the conversation
- Assistant asks "Would you like to proceed?" → suggest "Yes, proceed" / "No, not yet"
- Assistant asks "Do you accept these terms?" → suggest "Yes, I accept" / "No, decline"
- Assistant presents results → suggest logical next steps like "Connect another bank" / "Show details"

Rules:
- Each suggestion MUST be under 40 characters
- Generate exactly 2-3 suggestions
- Suggestions must directly respond to the assistant's question or offer logical next steps
- Write from the user's perspective (first person)
- If the assistant listed specific options (bank names, loan types), use those exact names in suggestions"""


class SuggestionResponse(BaseModel):
    """Structured output for reply suggestions."""

    suggestions: list[str] = Field(
        description="2-3 short follow-up reply suggestions, each under 40 characters",
    )


# Module-level LLM instance (same pattern as supervisor.py)
_suggestions_llm = ChatBedrockConverse(
    model=SUGGESTIONS_MODEL_ID,
    region_name=AWS_REGION,
    temperature=0.7,
)

_suggestions_llm_structured = _suggestions_llm.with_structured_output(SuggestionResponse)


def _get_recent_conversation(messages: list, limit: int = 4) -> list:
    """Extract last N human/AI messages, skipping tool messages.

    Filters out ToolMessages and AI messages that only contain tool_use
    blocks (no text content). This avoids Bedrock Converse validation
    errors from orphaned tool_use/toolResult pairs.
    """
    recent = []
    for msg in reversed(messages):
        if len(recent) >= limit:
            break

        if not hasattr(msg, "type"):
            continue

        if msg.type == "human":
            recent.append(HumanMessage(content=msg.content))
        elif msg.type == "ai" and isinstance(msg.content, str) and msg.content.strip():
            recent.append(AIMessage(content=msg.content))

    recent.reverse()
    return recent


async def generate_suggestions(
    messages: list,
    response_text: str,
) -> list[str]:
    """Generate 2-3 contextual reply suggestions based on conversation history.

    Uses a lightweight Haiku model for speed. On any failure, returns an
    empty list — suggestions are non-critical and must never block the
    main response.
    """
    try:
        context = _get_recent_conversation(messages)

        # Ensure the latest response is included
        if not context or context[-1].content != response_text:
            context.append(AIMessage(content=response_text))

        result: SuggestionResponse = await _suggestions_llm_structured.ainvoke(
            [SystemMessage(content=SYSTEM_PROMPT)] + context
        )

        # Filter to enforce length limit
        suggestions = [s for s in result.suggestions if len(s) <= 50]
        return suggestions[:3]

    except Exception:
        logger.warning("Failed to generate suggestions", exc_info=True)
        return []
