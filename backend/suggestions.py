"""Contextual reply suggestion generator.

After each assistant response, generates 2-3 short follow-up suggestions
using a lightweight Haiku model. Suggestions appear as clickable chips
in the frontend, guiding users through the demo flow.

Flow-state awareness: suggestions are constrained based on the current
conversation step (consent, financial advice, etc.)
to prevent dead-end chips and ensure contextual relevance.
"""

import logging

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from config import BEDROCK_CLIENT, SUGGESTIONS_MODEL_ID

logger = logging.getLogger(__name__)


# --- Flow-state-aware prompt system ---

_BASE_PROMPT = """You generate 2-3 short reply suggestions that DIRECTLY ANSWER the assistant's last message.

CRITICAL: Read the assistant's last message carefully. If it asks a question, your suggestions must be plausible ANSWERS to that question — not generic actions.

Rules:
- Each suggestion MUST be under 40 characters
- Generate exactly 2-3 suggestions
- Suggestions must directly respond to the assistant's question or offer logical next steps
- Write from the user's perspective (first person)
- If the assistant listed specific options (bank names, loan types), use those exact names in suggestions

BLOCKED (never suggest these regardless of context):
- Contacting a branch, scheduling a meeting, or signing up
- Checking email, checking spam folder, checking application status
- Any action the chatbot cannot actually perform
- Comparing Leafy Bank's rates against themselves"""

_FLOW_RULES = {
    "consent_flow": """CURRENT CONTEXT: The user is going through the consent/bank connection flow.

ALLOWED suggestions:
- Direct answers to the assistant's question (bank names, yes/no, accept/decline)
- If asked which bank: use exact bank names from the conversation
- If asked to accept terms: "I accept" / "I decline"
- If asked about loan type: use exact loan types mentioned

BLOCKED:
- Anything with: port, transfer, switch, move, apply, start, proceed
- Anything with: email, status""",

    "general": """ALLOWED suggestions:
- Direct answers to the assistant's question
- Logical next steps based on what was discussed

BLOCKED:
- Anything with: port, transfer, switch, move, apply, start, proceed
- Contacting a branch, scheduling a meeting
- Checking email, checking status
- Any action beyond viewing data or running analysis""",
}

# Hard blocklist — defense-in-depth filter applied after LLM generation
_BLOCKED_WORDS = {"email", "spam", "status", "application", "branch", "schedule", "meeting"}

# Fixed chips for the post-consent financial-advice flow. This flow is
# deterministic — the LLM is bypassed so ONLY these curated actions can ever
# appear once a consent is granted (no LLM-invented suggestions leak in).
_FINANCIAL_ADVICE_SUGGESTIONS = [
    "Show spending breakdown",
    "How much did I spend dining out?",
]


def _build_suggestions_prompt(flow_context: str) -> str:
    """Build a flow-state-aware system prompt for suggestion generation."""
    flow_rules = _FLOW_RULES.get(flow_context, _FLOW_RULES["general"])
    return f"{_BASE_PROMPT}\n\n{flow_rules}"


def _is_blocked(suggestion: str) -> bool:
    """Check if a suggestion contains any globally blocked words."""
    lower = suggestion.lower()
    return any(word in lower for word in _BLOCKED_WORDS)


class SuggestionResponse(BaseModel):
    """Structured output for reply suggestions."""

    suggestions: list[str] = Field(
        description="2-3 short follow-up reply suggestions, each under 40 characters",
    )


# Module-level LLM instance (same pattern as supervisor.py)
_suggestions_llm = ChatBedrockConverse(
    client=BEDROCK_CLIENT,
    model=SUGGESTIONS_MODEL_ID,
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
    flow_context: str = "general",
) -> list[str]:
    """Generate 2-3 contextual reply suggestions based on conversation history.

    Uses a lightweight Haiku model for speed. On any failure, returns an
    empty list — suggestions are non-critical and must never block the
    main response.

    Args:
        messages: Full conversation message history.
        response_text: The assistant's latest response text.
        flow_context: Current flow state for suggestion constraint rules.
            One of: consent_flow, financial_advice, general.
    """
    # Post-consent financial-advice flow is deterministic — return the fixed
    # chip set and never call the LLM, so no new suggestions can appear.
    if flow_context == "financial_advice":
        return list(_FINANCIAL_ADVICE_SUGGESTIONS)

    try:
        context = _get_recent_conversation(messages)

        # Ensure the latest response is included
        if not context or context[-1].content != response_text:
            context.append(AIMessage(content=response_text))

        prompt = _build_suggestions_prompt(flow_context)
        result: SuggestionResponse = await _suggestions_llm_structured.ainvoke(
            [SystemMessage(content=prompt)] + context
        )

        # Filter: length limit + blocklist
        suggestions = [s for s in result.suggestions if len(s) <= 50 and not _is_blocked(s)]
        return suggestions[:3]

    except Exception:
        logger.warning("Failed to generate suggestions", exc_info=True)
        return []
