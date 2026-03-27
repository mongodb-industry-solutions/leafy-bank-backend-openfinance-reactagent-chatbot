"""Admin endpoints for managing agent profiles and reloading the agent graph."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from graph import get_checkpointer, build_graph, profile_service

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateProfileRequest(BaseModel):
    agent_name: str
    profile_name: str
    system_prompt: str
    tool_config: dict = {}
    metadata: Optional[dict] = None


class UpdateProfileRequest(BaseModel):
    system_prompt: Optional[str] = None
    tool_config: Optional[dict] = None
    metadata: Optional[dict] = None


class ActivateProfileRequest(BaseModel):
    agent_name: str
    profile_name: str


@router.get("/profiles")
async def list_profiles(agent_name: Optional[str] = None):
    """List all profiles, optionally filtered by agent_name."""
    profiles = profile_service.list_profiles(agent_name)
    # Strip _id for JSON serialization
    for p in profiles:
        p["_id"] = str(p["_id"])
    return {"profiles": profiles}


@router.get("/profiles/{agent_name}/{profile_name}")
async def get_profile(agent_name: str, profile_name: str):
    """Get a specific profile."""
    profile = profile_service.collection.find_one(
        {"agent_name": agent_name, "profile_name": profile_name}
    )
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile not found: {agent_name}/{profile_name}")
    profile["_id"] = str(profile["_id"])
    return profile


@router.post("/profiles")
async def create_profile(request: CreateProfileRequest):
    """Create a new profile (inactive by default)."""
    try:
        profile_id = profile_service.create_profile(
            agent_name=request.agent_name,
            profile_name=request.profile_name,
            system_prompt=request.system_prompt,
            tool_config=request.tool_config,
            metadata=request.metadata,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"profile_id": profile_id, "status": "created"}


@router.put("/profiles/{agent_name}/{profile_name}")
async def update_profile(agent_name: str, profile_name: str, request: UpdateProfileRequest):
    """Update an existing profile's prompt and/or metadata."""
    modified = profile_service.update_profile(
        agent_name=agent_name,
        profile_name=profile_name,
        system_prompt=request.system_prompt,
        tool_config=request.tool_config,
        metadata=request.metadata,
    )
    if not modified:
        raise HTTPException(status_code=404, detail=f"Profile not found: {agent_name}/{profile_name}")
    return {"status": "updated", "agent_name": agent_name, "profile_name": profile_name}


@router.post("/profiles/activate")
async def activate_profile(request: ActivateProfileRequest):
    """Activate a profile for an agent (deactivates all others for that agent)."""
    success = profile_service.activate_profile(request.agent_name, request.profile_name)
    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"Profile not found: {request.agent_name}/{request.profile_name}",
        )
    return {"status": "activated", "agent_name": request.agent_name, "profile_name": request.profile_name}


@router.delete("/profiles/{agent_name}/{profile_name}")
async def delete_profile(agent_name: str, profile_name: str):
    """Delete a profile. Cannot delete the active or last profile for an agent."""
    success = profile_service.delete_profile(agent_name, profile_name)
    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete profile {agent_name}/{profile_name} — it may be active or the last one.",
        )
    return {"status": "deleted", "agent_name": agent_name, "profile_name": profile_name}


@router.post("/reload")
async def reload_graph(fastapi_request: Request):
    """Sync prompts from .md files into encrypted MongoDB, then rebuild the agent graph.

    1. Reads all prompt .md files and updates the active profiles in the DB.
    2. Rebuilds the agent graph from the freshly updated encrypted MongoDB.
    In-flight requests finish on the old graph; new requests use the new one.
    """
    sync_results = profile_service.sync_from_files()
    checkpointer = get_checkpointer()
    mcp_tools = getattr(fastapi_request.app.state, "mcp_tools", None)
    fastapi_request.app.state.agent = build_graph(checkpointer, mcp_tools=mcp_tools)
    logger.info("Agent graph reloaded with fresh profiles from encrypted MongoDB")
    return {"status": "reloaded", "synced_profiles": sync_results}