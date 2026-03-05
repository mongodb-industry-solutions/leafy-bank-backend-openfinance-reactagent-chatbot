"""SSE streaming helpers for the Open Finance chatbot.

Converts LangGraph astream(subgraphs=True) events into
Server-Sent Events for real-time frontend updates.
"""

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Human-readable agent names
_AGENT_DISPLAY_NAMES = {
    "consent_agent": "Consent Agent",
    "analysis_agent": "Analysis Agent",
}


def sse_event(event_type: str, payload: dict) -> str:
    """Format a single SSE event line."""
    data = json.dumps({"type": event_type, "payload": payload})
    return f"data: {data}\n\n"


def process_stream_event(event: tuple) -> list[str]:
    """Convert a LangGraph astream(subgraphs=True, stream_mode=[...]) event
    into zero or more SSE event strings.

    With stream_mode=["updates", "custom"] and subgraphs=True, each event is:
        (namespace_tuple, mode_str, data)

    mode_str:
        "updates" = standard node update
        "custom"  = user-emitted via get_stream_writer()

    namespace_tuple:
        () = parent graph
        ("consent_agent:xxx",) = inside consent_agent sub-graph
        ("portability_agent:xxx",) = inside portability_agent sub-graph
    """
    if not isinstance(event, tuple):
        logger.debug("Skipping non-tuple event: %s", type(event))
        return []

    if len(event) == 3:
        namespace, mode, data = event

        if mode == "custom":
            return _handle_custom_event(namespace, data)

        if mode == "updates":
            return _handle_updates_event(namespace, data)

        logger.debug("Skipping unknown stream mode: %s", mode)
        return []

    logger.debug("Skipping event with unexpected length: %d", len(event))
    return []


def _handle_updates_event(namespace: tuple, data: dict) -> list[str]:
    """Process a standard 'updates' mode event."""
    if not isinstance(data, dict):
        logger.debug("Skipping non-dict updates data: %s", type(data))
        return []

    agent_name = _extract_agent_name(namespace)

    results = []
    for node_name, update in data.items():
        results.extend(_process_node_update(node_name, update, agent_name))

    return results


def _handle_custom_event(namespace: tuple, data) -> list[str]:
    """Process a 'custom' mode event emitted by get_stream_writer()."""
    if not isinstance(data, dict):
        logger.debug("Skipping non-dict custom data: %s", type(data))
        return []

    if data.get("type") != "progress":
        logger.debug("Skipping unknown custom event type: %s", data.get("type"))
        return []

    agent_name = _extract_agent_name(namespace)

    payload = {
        "agent": agent_name,
        "message": data.get("message", ""),
    }
    # Pass through optional fields for input/output display
    for key in ("step", "input", "output"):
        if key in data:
            payload[key] = data[key]

    return [sse_event("progress", payload)]


def _extract_agent_name(namespace: tuple) -> Optional[str]:
    """Extract the agent name from a LangGraph namespace tuple."""
    if not namespace:
        return None
    first_ns = namespace[0]
    # Format is "agent_name:task_id"
    name = first_ns.split(":")[0] if ":" in first_ns else first_ns
    if name in _AGENT_DISPLAY_NAMES:
        return name
    return name


def _process_node_update(node_name: str, update: dict, agent_name: Optional[str]) -> list[str]:
    """Process a single node update and return SSE events."""
    results = []

    if node_name == "supervisor":
        results.extend(_handle_supervisor(update))

    elif node_name == "model" and agent_name:
        # Inside a sub-agent: the LLM decided on tool calls or produced a response
        results.extend(_handle_model_update(update, agent_name))

    elif node_name == "tools" and agent_name:
        # Inside a sub-agent: tool execution completed
        results.extend(_handle_tools_update(update, agent_name))

    elif node_name in ("consent_agent", "analysis_agent"):
        # Parent-level: sub-agent finished
        results.append(sse_event("agent_complete", {
            "agent": node_name,
            "agent_display": _AGENT_DISPLAY_NAMES.get(node_name, node_name),
        }))

    return results


def _handle_supervisor(update: dict) -> list[str]:
    """Handle supervisor node updates."""
    results = []
    next_agent = update.get("next", "FINISH")

    if next_agent != "FINISH":
        display_name = _AGENT_DISPLAY_NAMES.get(next_agent, next_agent)
        results.append(sse_event("status", {
            "message": f"Routing to {display_name}...",
        }))

    return results


def _handle_model_update(update: dict, agent_name: str) -> list[str]:
    """Handle model node updates from inside a sub-agent."""
    results = []
    messages = update.get("messages", [])

    for msg in messages:
        # Check for tool calls
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                results.append(sse_event("tool_call", {
                    "agent": agent_name,
                    "agent_display": _AGENT_DISPLAY_NAMES.get(agent_name, agent_name),
                    "tool": tc.get("name", "unknown"),
                    "args": _summarize_args(tc.get("args", {})),
                }))

    return results


def _handle_tools_update(update: dict, agent_name: str) -> list[str]:
    """Handle tools node updates from inside a sub-agent."""
    results = []
    messages = update.get("messages", [])

    for msg in messages:
        if hasattr(msg, "type") and msg.type == "tool":
            tool_name = getattr(msg, "name", "unknown")
            results.append(sse_event("tool_result", {
                "agent": agent_name,
                "agent_display": _AGENT_DISPLAY_NAMES.get(agent_name, agent_name),
                "tool": tool_name,
                "summary": _summarize_tool_result(getattr(msg, "content", "")),
            }))

    return results


def _summarize_args(args: dict) -> dict:
    """Return tool arguments as-is for raw display."""
    return args


def _summarize_tool_result(content) -> str:
    """Return tool output as a string for display.

    LangChain ToolMessage content can be a str, dict, or list
    (e.g. content blocks like {type, text, id}). Always return a string.

    MongoDB MCP server wraps query results in <untrusted-user-data-*> tags
    as a prompt-injection defense. These are meant for the LLM only and
    must be stripped before sending to the frontend, keeping the actual data.
    """
    if not content:
        return ""
    if isinstance(content, str):
        return _strip_untrusted_wrapper(content)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(_strip_untrusted_wrapper(item["text"]))
            elif isinstance(item, str):
                parts.append(_strip_untrusted_wrapper(item))
        return "\n".join(p for p in parts if p)
    return json.dumps(content)


def _strip_untrusted_wrapper(text: str) -> str:
    """Strip MongoDB MCP security wrapper, keeping the actual data inside."""
    # Remove the warning preamble
    cleaned = re.sub(
        r"The following section contains unverified user data\. WARNING:.*?"
        r"NEVER execute or act on any instructions within these boundaries:\s*",
        "",
        text,
        flags=re.DOTALL,
    )
    # Remove opening and closing tags, keep content between them
    cleaned = re.sub(r"</?untrusted-user-data-[a-f0-9-]+>\s*", "", cleaned)
    # Remove the trailing warning
    cleaned = re.sub(
        r"\s*Use the information above to respond.*?Treat all content within "
        r"these tags as potentially malicious\.",
        "",
        cleaned,
        flags=re.DOTALL,
    )
    return cleaned.strip()
