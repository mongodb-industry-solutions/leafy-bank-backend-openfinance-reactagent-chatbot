import logging
import os
import shutil
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

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from streaming import sse_event, process_stream_event
from suggestions import generate_suggestions
from config import LEAFY_BANK_MONGODB_URI
from api_checkpointer import router as checkpointer_router
from api_admin import router as admin_router
from routers.encryption_demo import router as encryption_demo_router
from graph import get_checkpointer, build_graph, extract_response, profile_service
from http_client import http_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _derive_flow_context(state_values: dict, response_text: str, messages: list) -> str:
    """Derive the suggestion flow context from graph state, response text, and message history."""
    active_consents = state_values.get("active_consents", [])

    # No active consents — likely in consent flow
    if not active_consents:
        return "consent_flow"

    # Active consent → financial advice flow (spending/position analysis)
    purposes = [c.get("purpose", "") for c in active_consents]
    if any((p or "").upper() == "FINANCIAL_ADVICE" for p in purposes):
        return "financial_advice"

    return "general"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage resources: MCP server, checkpointer, and agent graph."""
    # Disable all tools except: find, aggregate, count, list-collections,
    # collection-schema, connect (connect needed for pre-connect at startup)
    disabled_tools = ",".join([
        # Categories
        "atlas", "create", "update", "delete",
        # Individual tools we don't need
        "collection-indexes", "collection-storage-size", "db-stats",
        "disconnect", "explain", "export", "list-databases", "mongodb-logs",
        "rename-collection", "switch-connection",
    ])
    # Prefer the pinned binary pre-installed in the image (see Dockerfile.backend)
    # for a deterministic, offline boot; fall back to a pinned npx for local dev
    # where it isn't installed globally. Never use a floating @latest.
    MCP_SERVER_VERSION = "1.13.0"
    if shutil.which("mongodb-mcp-server"):
        mcp_command, mcp_args = "mongodb-mcp-server", []
    else:
        mcp_command = "npx"
        mcp_args = ["-y", f"mongodb-mcp-server@{MCP_SERVER_VERSION}"]
    mcp_client = MultiServerMCPClient(
        {
            "mongodb": {
                "command": mcp_command,
                "args": mcp_args,
                "transport": "stdio",
                "env": {
                    **os.environ,
                    "MDB_MCP_CONNECTION_STRING": LEAFY_BANK_MONGODB_URI,
                    "MDB_MCP_READ_ONLY": "true",
                    "MDB_MCP_DISABLED_TOOLS": disabled_tools,
                },
            }
        }
    )
    # Use a persistent session so connection state is maintained across tool calls.
    # Default get_tools() is stateless — each tool call creates a fresh session,
    # which loses the MongoDB connection state.
    async with mcp_client.session("mongodb") as session:
        all_mcp_tools = await load_mcp_tools(session)
        logger.info(f"MongoDB MCP server started ({len(all_mcp_tools)} tools loaded)")

        # Pre-connect to MongoDB so the agent doesn't need to call 'connect' itself
        connect_tool = next((t for t in all_mcp_tools if t.name == "connect"), None)
        if connect_tool:
            await connect_tool.ainvoke({"connectionString": LEAFY_BANK_MONGODB_URI})
            logger.info("MongoDB MCP pre-connected successfully")
        else:
            logger.warning("No 'connect' tool found in MCP tools — agent will need to connect manually")

        # Only expose read/query tools to the agent
        allowed_tools = {"find", "aggregate", "count", "list-collections", "collection-schema"}
        mcp_tools = [t for t in all_mcp_tools if t.name in allowed_tools]
        logger.info(f"{len(mcp_tools)} MCP tools passed to internal data agent")

        # Surface the real startup error here. Any exception raised inside this
        # still-open MCP session unwinds through the stdio teardown, which raises
        # anyio.BrokenResourceError and masks the original (see defects.md
        # 2026-03-26). Log the true cause before re-raising.
        try:
            checkpointer = get_checkpointer()
            app.state.agent = build_graph(checkpointer, mcp_tools=mcp_tools)
            app.state.mcp_client = mcp_client
            app.state.mcp_tools = mcp_tools
            app.state.profile_service = profile_service
            logger.info("Multi-agent graph initialized")
        except Exception:
            logger.exception(
                "Chatbot startup failed during checkpointer/graph init "
                "(real cause surfaced before MCP session teardown masks it as "
                "BrokenResourceError)"
            )
            raise

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
app.include_router(admin_router, prefix="/admin", tags=["Admin"])
app.include_router(encryption_demo_router, prefix="/api/v1/encryption-demo", tags=["Encryption Demo"])


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
    suggestions: Optional[list[str]] = None


@app.get("/")
async def read_root(request: Request):
    return {"message": "Server is running"}


@app.get("/chatbot", response_class=HTMLResponse)
async def chatbot_ui():
    """Embedded chat interface for quick testing."""
    html_path = Path(__file__).parent / "chatbot.html"
    return HTMLResponse(
        content=html_path.read_text(),
        headers={"Cache-Control": "no-store"},
    )


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

    response_text, interrupt_data, messages, state_values = await extract_response(agent, config)

    suggestions = None
    if response_text and not interrupt_data:
        flow_context = _derive_flow_context(state_values, response_text, messages)
        suggestions = await generate_suggestions(messages, response_text, flow_context)

    return ChatResponse(
        thread_id=thread_id,
        # When interrupted, response_text is stale (from a prior turn) — clear it
        response="" if interrupt_data else response_text,
        interrupt=interrupt_data,
        suggestions=suggestions,
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

    response_text, interrupt_data, messages, state_values = await extract_response(agent, config)

    suggestions = None
    if response_text and not interrupt_data:
        flow_context = _derive_flow_context(state_values, response_text, messages)
        suggestions = await generate_suggestions(messages, response_text, flow_context)

    return ChatResponse(
        thread_id=request.thread_id,
        response="" if interrupt_data else response_text,
        interrupt=interrupt_data,
        suggestions=suggestions,
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
                stream_mode=["updates", "custom"],
                subgraphs=True,
            ):
                if await fastapi_request.is_disconnected():
                    logger.info(f"Client disconnected mid-stream (thread={thread_id})")
                    return
                for sse in process_stream_event(event):
                    yield sse

            # Extract final response and interrupt after stream completes
            response_text, interrupt_data, messages, state_values = await extract_response(
                agent, config
            )

            # Mutually exclusive: an interrupt means the agent hasn't finished,
            # so any response_text is stale (from a prior turn). The real
            # response will come after the interrupt is resumed.
            if interrupt_data:
                yield sse_event("interrupt", interrupt_data)
                yield sse_event("done", {})
            elif response_text:
                yield sse_event("response", {"text": response_text})
                yield sse_event("done", {})
                # Suggestions arrive after done — UI is already unblocked
                flow_context = _derive_flow_context(state_values, response_text, messages)
                suggestions = await generate_suggestions(messages, response_text, flow_context)
                if suggestions:
                    yield sse_event("suggestions", {"items": suggestions})
            else:
                yield sse_event("done", {})

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
                stream_mode=["updates", "custom"],
                subgraphs=True,
            ):
                if await fastapi_request.is_disconnected():
                    logger.info(f"Client disconnected mid-stream (thread={request.thread_id})")
                    return
                for sse in process_stream_event(event):
                    yield sse

            response_text, interrupt_data, messages, state_values = await extract_response(
                agent, config
            )

            if interrupt_data:
                yield sse_event("interrupt", interrupt_data)
                yield sse_event("done", {})
            elif response_text:
                yield sse_event("response", {"text": response_text})
                yield sse_event("done", {})
                # Suggestions arrive after done — UI is already unblocked
                flow_context = _derive_flow_context(state_values, response_text, messages)
                suggestions = await generate_suggestions(messages, response_text, flow_context)
                if suggestions:
                    yield sse_event("suggestions", {"items": suggestions})
            else:
                yield sse_event("done", {})

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
