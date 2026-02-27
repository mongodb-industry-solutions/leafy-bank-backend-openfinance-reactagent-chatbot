"""Parent StateGraph — orchestrates supervisor, consent agent, and analysis agent."""

import logging
from typing import Optional

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.mongodb import MongoDBSaver

from config import (
    DATABASE_NAME,
    CHECKPOINTS_AIO_COLLECTION,
    CHECKPOINTS_WRITES_AIO_COLLECTION,
)
from state import AgentState
from agent.db.mdb import MongoDBConnector
from agent.consent_agent import create_consent_agent
from agent.analysis_agent import create_analysis_agent
from agent.supervisor import supervisor_node

logger = logging.getLogger(__name__)

# Shared MongoDB connector — single client for the app
db = MongoDBConnector()


def get_checkpointer() -> MongoDBSaver:
    """Create and return a MongoDBSaver using the shared MongoDB client."""
    return MongoDBSaver(
        client=db.client,
        db_name=DATABASE_NAME,
        checkpoint_collection_name=CHECKPOINTS_AIO_COLLECTION,
        writes_collection_name=CHECKPOINTS_WRITES_AIO_COLLECTION,
    )


def _route_from_supervisor(state: dict) -> str:
    """Read the supervisor's routing decision from state."""
    return state.get("next", "FINISH")


def build_graph(checkpointer: MongoDBSaver):
    """Build and compile the multi-agent graph."""
    consent_agent = create_consent_agent()
    analysis_agent = create_analysis_agent()

    workflow = StateGraph(AgentState)

    # Nodes
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("consent_agent", consent_agent)
    workflow.add_node("analysis_agent", analysis_agent)

    # Edges
    workflow.add_edge(START, "supervisor")
    workflow.add_conditional_edges(
        "supervisor",
        _route_from_supervisor,
        {
            "consent_agent": "consent_agent",
            "analysis_agent": "analysis_agent",
            "FINISH": END,
        },
    )
    workflow.add_edge("consent_agent", "supervisor")
    workflow.add_edge("analysis_agent", "supervisor")

    graph = workflow.compile(checkpointer=checkpointer)

    logger.info("Multi-agent graph built successfully")
    return graph


async def extract_response(agent, config: dict) -> tuple[str, Optional[dict]]:
    """Extract the agent's last AI message and any pending interrupt from graph state."""
    state = await agent.aget_state(config)

    # Check for pending interrupts
    interrupt_data = None
    if state.next:
        for task in state.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                interrupt_data = task.interrupts[0].value
                break

    # Get the last AI message
    messages = state.values.get("messages", [])
    response_text = ""
    for msg in reversed(messages):
        if hasattr(msg, "type") and msg.type == "ai" and msg.content:
            response_text = msg.content
            break

    return response_text, interrupt_data
