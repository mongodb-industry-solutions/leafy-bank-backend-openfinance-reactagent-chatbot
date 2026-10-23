"""Shared state definition for the multi-agent graph."""

from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class ConsentInfo(TypedDict):
    consent_id: str
    purpose: str | None
    institution: str


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    next: str  # "consent_agent" | "financial_advice_agent" | "FINISH"
    active_consents: list[ConsentInfo]  # Multiple banks
