"""Contextual reply suggestion generator.

After each assistant response, generates 2-3 short follow-up suggestions
using a lightweight Haiku model. Suggestions appear as clickable chips
in the frontend, guiding users through the demo flow.

Flow-state awareness: suggestions are constrained based on the current
conversation step (consent, financial advice, etc.)
to prevent dead-end chips and ensure contextual relevance.
"""

import logging
import re

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

# --- Deterministic consent/connect chips ---------------------------------
#
# Suggestion chips are click-to-send text: a chip labelled "I accept" sends
# "I accept" as the user's next message. For the consent/connect flow the
# valid next actions are FIXED per step, so we bypass the LLM entirely and
# emit them deterministically. This is keyed off the last CONSENT TOOL the
# agent invoked in the current turn (a reliable signal) rather than prose
# matching, which is fragile (see defects.md 2026-03-31). Two tool-less
# steps fall back to narrow, constrained prose checks.

# Permissions just shown → user can accept, trim scope, or decline.
_CONSENT_REVIEW_SUGGESTIONS = ["I accept", "Remove permissions", "I decline"]

# "Remove permissions" clarifying step: the agent lists current permissions and
# asks which to remove — a tool-less prose turn, so it isn't caught by the
# consent-tool branches. Offer a remove chip per REMOVABLE permission. Account
# info (ACCOUNTS_READ) is foundational and mandatory, so it is never offered.
_REMOVE_PROMPT_PHRASES = (
    "would you like to remove",
    "which one",  # "Which one(s) would you like to remove?"
)
# Friendly permission name (as printed to the user) → remove chip.
_REMOVABLE_PERMISSION_CHIPS = {
    "balances": "Remove Balances",
    "transaction history": "Remove Transaction history",
    "loans & credit products": "Remove Loans & credit products",
    "product details": "Remove Loans & credit products",
}
# Duration/proceed step (no tool call) → accept or decline.
_CONSENT_ACCEPT_SUGGESTIONS = ["I accept", "I decline"]

# Consent tools we key step detection on.
_CONSENT_TOOLS = {
    "list_institutions",
    "get_default_permissions",
    "create_consent",
    "approve_consent",
    "request_bank_login",
    "fetch_and_cache_data",
    "list_user_consents",
    "revoke_consent",
    "get_consent",
}

# Exact prefix emitted by the list_institutions tool.
_INSTITUTIONS_PREFIX = "Open Finance authorized institutions are:"

# Accept/decline prompt markers. consent.md mandates every consent-step
# response end with an accept/decline prompt, so these are reliable for the
# one step that carries no tool call (duration/proceed acceptance).
_ACCEPT_DECLINE_PHRASES = (
    "do you accept",
    "do you agree",
    "accept these terms",
    "accept these permissions",
    "wish to proceed",
    "like to proceed",
    "ready to proceed",
    "proceed with the secure connection",
    "proceed with the connection",
)


def _find_last_consent_tool(messages: list) -> str | None:
    """Name of the most recent consent-tool call in the CURRENT turn.

    Scans backward and stops at the previous human message so only the
    agent's latest turn is considered. Returns None if the turn invoked no
    consent tool.
    """
    for msg in reversed(messages):
        mtype = getattr(msg, "type", None)
        if mtype == "human":
            break
        if mtype == "tool" and getattr(msg, "name", None) in _CONSENT_TOOLS:
            return msg.name
    return None


def _extract_institutions(messages: list) -> list[str]:
    """Parse bank names from the most recent list_institutions tool output.

    The names come from the tool's own output (not guessed), so the chip
    vocabulary is deterministic.
    """
    for msg in reversed(messages):
        if getattr(msg, "type", None) == "tool" and getattr(msg, "name", None) == "list_institutions":
            content = msg.content if isinstance(msg.content, str) else ""
            if _INSTITUTIONS_PREFIX in content:
                tail = content.split(_INSTITUTIONS_PREFIX, 1)[1]
                return [n.strip(" .") for n in tail.split(",") if n.strip(" .")]
            return []
    return []


def _connect_chips(institutions: list[str], connected: list[str]) -> list[str]:
    """`Connect to <Bank>` chips for banks not yet connected this session."""
    connected_lower = {c.lower() for c in connected}
    return [
        f"Connect to {name}"
        for name in institutions
        if name.lower() not in connected_lower
    ][:3]


def _has_accept_decline_prompt(response_text: str) -> bool:
    lower = response_text.lower()
    return any(phrase in lower for phrase in _ACCEPT_DECLINE_PHRASES)


def _remove_permission_chips(response_text: str) -> list[str]:
    """Chips for the "which permission to remove?" step.

    Returns a `Remove <permission>` chip for each removable permission the
    agent listed in its response, or [] if this isn't a remove-scope prompt.
    Account info is mandatory, so it never appears here.
    """
    lower = response_text.lower()
    if not any(phrase in lower for phrase in _REMOVE_PROMPT_PHRASES):
        return []
    chips: list[str] = []
    for name, chip in _REMOVABLE_PERMISSION_CHIPS.items():
        if name in lower and chip not in chips:
            chips.append(chip)
    return chips[:3]


def _recommended_banks(
    response_text: str, institutions: list[str], connected: list[str]
) -> list[str]:
    """Banks the agent recommends connecting to in a tool-less prose turn.

    Constrained to the known institution vocabulary and requires the word
    "connect" in the response to avoid matching incidental bank mentions.
    """
    lower = response_text.lower()
    # Match "connect" as a whole word only — avoid "connection"/"connected"
    # which appear in the proceed/confirmation copy and would false-positive.
    if not institutions or not re.search(r"\bconnect\b", lower):
        return []
    connected_lower = {c.lower() for c in connected}
    return [
        name
        for name in institutions
        if name.lower() in lower and name.lower() not in connected_lower
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
    active_consents: list | None = None,
) -> list[str]:
    """Resolve reply-suggestion chips for the latest assistant turn.

    Deterministic-first: consent/connect steps have a FIXED set of valid
    next actions, so those chips are emitted directly (LLM bypassed), keyed
    off the last consent tool the agent invoked. Only genuinely open turns
    fall through to the Haiku LLM. On any LLM failure, returns an empty list
    — suggestions are non-critical and must never block the main response.

    Args:
        messages: Full conversation message history.
        response_text: The assistant's latest response text.
        active_consents: Consents created this session (from graph state),
            used to filter already-connected banks and detect the
            financial-advice flow.
    """
    active_consents = active_consents or []
    connected = [c.get("institution", "") for c in active_consents]
    institutions = _extract_institutions(messages)
    last_tool = _find_last_consent_tool(messages)

    # Interrupt-driving tools run their own dedicated UI (approval modal,
    # bank-login tab) — chips would compete with it.
    if last_tool in {"approve_consent", "request_bank_login"}:
        return []

    # Bank list just presented → offer connect chips for unconnected banks.
    if last_tool == "list_institutions":
        chips = _connect_chips(institutions, connected)
        if chips:
            return chips

    # Permissions just presented → accept / remove-scope / decline.
    if last_tool == "get_default_permissions":
        return list(_CONSENT_REVIEW_SUGGESTIONS)

    # Data cached (connection complete) → financial-advice actions, plus a
    # generic "connect another bank" option when one is still available. The
    # chip routes back through list_institutions so the user picks from the
    # remaining banks rather than being steered to a specific one.
    if last_tool == "fetch_and_cache_data":
        chips = list(_FINANCIAL_ADVICE_SUGGESTIONS)
        if _connect_chips(institutions, connected):
            chips.append("Connect another bank")
        return chips

    # --- prose fallbacks for tool-less consent turns ---

    # Duration/proceed acceptance step carries no tool call.
    if _has_accept_decline_prompt(response_text):
        return list(_CONSENT_ACCEPT_SUGGESTIONS)

    # "Which permission would you like to remove?" step (no tool call). Must be
    # checked before the financial-advice fallback below, which otherwise
    # hijacks this consent question with advice chips when a prior
    # FINANCIAL_ADVICE consent exists.
    remove_chips = _remove_permission_chips(response_text)
    if remove_chips:
        return remove_chips

    # Post-connect: agent recommends connecting other named banks.
    recommended = _recommended_banks(response_text, institutions, connected)
    if recommended:
        return [f"Connect to {b}" for b in recommended][:3]

    # Ongoing financial-advice turns (consent already granted, no consent
    # step in progress) keep the fixed advice chips.
    purposes = [(c.get("purpose") or "").upper() for c in active_consents]
    if "FINANCIAL_ADVICE" in purposes:
        return list(_FINANCIAL_ADVICE_SUGGESTIONS)

    # --- non-deterministic fallback: Haiku LLM ---
    flow_context = "consent_flow" if not active_consents else "general"
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
