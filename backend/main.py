import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

from streaming import sse_event, process_stream_event

from api_checkpointer import router as checkpointer_router
from graph import get_checkpointer, build_graph, extract_response
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
    profile: Optional[str] = None


class ResumeRequest(BaseModel):
    thread_id: str
    user_id: str
    resume_data: dict
    profile: Optional[str] = None


class ChatResponse(BaseModel):
    thread_id: str
    response: str
    interrupt: Optional[dict] = None


@app.get("/")
async def read_root(request: Request):
    return {"message": "Server is running"}


@app.get("/chatbot", response_class=HTMLResponse)
async def chatbot_ui():
    """Embedded chat interface for quick testing."""
    html_path = Path(__file__).parent / "chatbot.html"
    return HTMLResponse(content=html_path.read_text())


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, fastapi_request: Request):
    """Send a message to the consent agent."""
    thread_id = request.thread_id or str(uuid.uuid4())
    agent = fastapi_request.app.state.agent

    config = {
        "configurable": {
            "thread_id": thread_id,
            "user_id": request.user_id,
            "profile": request.profile,
        }
    }

    await agent.ainvoke(
        {"messages": [HumanMessage(content=request.message)]},
        config,
    )

    response_text, interrupt_data = await extract_response(agent, config)

    return ChatResponse(
        thread_id=thread_id,
        # When interrupted, response_text is stale (from a prior turn) — clear it
        response="" if interrupt_data else response_text,
        interrupt=interrupt_data,
    )


@app.post("/chat/resume", response_model=ChatResponse)
async def chat_resume(request: ResumeRequest, fastapi_request: Request):
    """Resume a conversation after an interrupt (e.g., bank login)."""
    agent = fastapi_request.app.state.agent

    config = {
        "configurable": {
            "thread_id": request.thread_id,
            "user_id": request.user_id,
            "profile": request.profile,
        }
    }

    await agent.ainvoke(
        Command(resume=request.resume_data),
        config,
    )

    response_text, interrupt_data = await extract_response(agent, config)

    return ChatResponse(
        thread_id=request.thread_id,
        response="" if interrupt_data else response_text,
        interrupt=interrupt_data,
    )


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest, fastapi_request: Request):
    """Stream intermediate agent steps via SSE."""
    thread_id = request.thread_id or str(uuid.uuid4())
    agent = fastapi_request.app.state.agent

    config = {
        "configurable": {
            "thread_id": thread_id,
            "user_id": request.user_id,
            "profile": request.profile,
        }
    }

    async def event_generator():
        yield sse_event("thread_id", {"thread_id": thread_id})

        try:
            async for event in agent.astream(
                {"messages": [HumanMessage(content=request.message)]},
                config,
                stream_mode="updates",
                subgraphs=True,
            ):
                if await fastapi_request.is_disconnected():
                    logger.info(f"Client disconnected mid-stream (thread={thread_id})")
                    return
                for sse in process_stream_event(event):
                    yield sse

            # Extract final response and interrupt after stream completes
            response_text, interrupt_data = await extract_response(
                agent, config
            )

            # Mutually exclusive: an interrupt means the agent hasn't finished,
            # so any response_text is stale (from a prior turn). The real
            # response will come after the interrupt is resumed.
            if interrupt_data:
                yield sse_event("interrupt", interrupt_data)
            elif response_text:
                yield sse_event("response", {"text": response_text})

        except Exception as e:
            logger.error(f"Stream error: {e}", exc_info=True)
            yield sse_event("error", {"message": str(e)})

        yield sse_event("done", {})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/chat/stream/resume")
async def chat_stream_resume(request: ResumeRequest, fastapi_request: Request):
    """Stream after resuming from an interrupt (e.g., bank login)."""
    agent = fastapi_request.app.state.agent

    config = {
        "configurable": {
            "thread_id": request.thread_id,
            "user_id": request.user_id,
            "profile": request.profile,
        }
    }

    async def event_generator():
        yield sse_event("thread_id", {"thread_id": request.thread_id})

        try:
            async for event in agent.astream(
                Command(resume=request.resume_data),
                config,
                stream_mode="updates",
                subgraphs=True,
            ):
                if await fastapi_request.is_disconnected():
                    logger.info(f"Client disconnected mid-stream (thread={request.thread_id})")
                    return
                for sse in process_stream_event(event):
                    yield sse

            response_text, interrupt_data = await extract_response(
                agent, config
            )

            if interrupt_data:
                yield sse_event("interrupt", interrupt_data)
            elif response_text:
                yield sse_event("response", {"text": response_text})

        except Exception as e:
            logger.error(f"Stream resume error: {e}", exc_info=True)
            yield sse_event("error", {"message": str(e)})

        yield sse_event("done", {})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
