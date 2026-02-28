"""Shared state definition for the multi-agent graph."""

from typing import Annotated, Optional

from typing_extensions import TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    next: str  # "consent_agent" | "portability_agent" | "internal_data_agent" | "FINISH"
    active_consent_id: Optional[str]
    active_consent_purpose: Optional[str]
    source_institution: Optional[str]
