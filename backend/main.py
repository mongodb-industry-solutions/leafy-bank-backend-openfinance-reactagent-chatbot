import logging
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

from api_checkpointer import router as checkpointer_router
from graph import get_checkpointer, build_graph
from http_client import http_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage resources: checkpointer and agent graph."""
    checkpointer = get_checkpointer()
    app.state.agent = build_graph(checkpointer)
    logger.info("Multi-agent graph initialized")
    yield
    await http_client.aclose()
    logger.info("HTTP client closed")


app = FastAPI(
    title="Open Finance Chatbot API",
    version="0.1.0",
    description="Multi-agent chatbot for Open Finance consent management and financial advice",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(checkpointer_router)


class ChatRequest(BaseModel):
    thread_id: Optional[str] = None
    user_id: str
    message: str


class ResumeRequest(BaseModel):
    thread_id: str
    resume_data: dict


class ChatResponse(BaseModel):
    thread_id: str
    response: str
    interrupt: Optional[dict] = None


async def _extract_response(agent, config: dict) -> tuple[str, Optional[dict]]:
    """Extract the agent's last message and check for pending interrupts."""
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


@app.get("/")
async def read_root(request: Request):
    return {"message": "Server is running"}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Send a message to the consent agent."""
    thread_id = request.thread_id or str(uuid.uuid4())
    agent = request.app.state.agent

    config = {
        "configurable": {
            "thread_id": thread_id,
            "user_id": request.user_id,
        }
    }

    await agent.ainvoke(
        {"messages": [HumanMessage(content=request.message)]},
        config,
    )

    response_text, interrupt_data = await _extract_response(agent, config)

    return ChatResponse(
        thread_id=thread_id,
        response=response_text,
        interrupt=interrupt_data,
    )


@app.post("/chat/resume", response_model=ChatResponse)
async def chat_resume(request: ResumeRequest):
    """Resume a conversation after an interrupt (e.g., bank login)."""
    agent = request.app.state.agent

    config = {
        "configurable": {
            "thread_id": request.thread_id,
        }
    }

    await agent.ainvoke(
        Command(resume=request.resume_data),
        config,
    )

    response_text, interrupt_data = await _extract_response(agent, config)

    return ChatResponse(
        thread_id=request.thread_id,
        response=response_text,
        interrupt=interrupt_data,
    )
