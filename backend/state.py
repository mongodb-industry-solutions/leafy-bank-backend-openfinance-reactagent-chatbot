"""Shared state definition for the multi-agent graph."""

from typing import Annotated, Optional

from typing_extensions import TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class ConsentInfo(TypedDict):
    consent_id: str
    purpose: Optional[str]
    institution: str


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    next: str  # "consent_agent" | "financial_advice_agent" | "FINISH"
    active_consents: list[ConsentInfo]  # Multiple banks
