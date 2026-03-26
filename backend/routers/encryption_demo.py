"""
Encryption Demo Router — Agent Profiles

Demonstrates MongoDB Queryable Encryption by showing the same agent profile
as seen by the encrypted client (decrypted) vs a plain client (encrypted binary blobs).

Demo story: "MongoDB QE protects AI agent configuration data so even with a
database breach, an attacker can't read or tamper with the instructions
governing thousands of agent interactions."
"""

import json
import logging

from fastapi import APIRouter, HTTPException

from config import DATABASE_NAME, AGENT_PROFILES_COLLECTION
from graph import profile_service, db

logger = logging.getLogger(__name__)

router = APIRouter()

ENCRYPTED_FIELD_PATHS = ["agent_name", "system_prompt", "tool_config"]


def _serialize_value(val):
    """Convert a value to a JSON-safe representation."""
    type_name = type(val).__name__
    if type_name == "Binary":
        return f"Binary({len(val)} bytes)"
    if isinstance(val, str) and len(val) > 200:
        return val[:200] + "..."
    return val


@router.get("/compare/{agent_name}")
async def compare_encrypted_vs_plain(agent_name: str):
    """Show the same agent profile through two lenses:

    1. Encrypted client: fields are decrypted (normal text)
    2. Plain client: sensitive fields appear as Binary blobs

    This proves encryption is active on disk and the prompt content
    is protected from unauthorized access.
    """
    # Read via encrypted client (decrypted view)
    decrypted_doc = profile_service.get_active_profile(agent_name)
    if not decrypted_doc:
        raise HTTPException(status_code=404, detail=f"No active profile for agent '{agent_name}'")

    # Read via plain client (raw/encrypted view) — use _id to find the same doc
    plain_coll = db.client[DATABASE_NAME][AGENT_PROFILES_COLLECTION]
    raw_doc = plain_coll.find_one({"_id": decrypted_doc["_id"]})

    # Build field-by-field comparison
    fields_comparison = {}
    all_encrypted = True
    for field_path in ENCRYPTED_FIELD_PATHS:
        decrypted_val = decrypted_doc.get(field_path)
        raw_val = raw_doc.get(field_path) if raw_doc else None

        is_encrypted = type(raw_val).__name__ == "Binary"
        if not is_encrypted:
            all_encrypted = False

        fields_comparison[field_path] = {
            "decrypted_value": _serialize_value(decrypted_val),
            "raw_type": type(raw_val).__name__,
            "raw_value": _serialize_value(raw_val),
            "is_encrypted": is_encrypted,
        }

    return {
        "agent_name": agent_name,
        "profile_name": decrypted_doc.get("profile_name"),
        "encryption_verified": all_encrypted,
        "encrypted_fields": fields_comparison,
        "plaintext_fields": {
            "profile_name": decrypted_doc.get("profile_name"),
            "is_active": decrypted_doc.get("is_active"),
            "version": decrypted_doc.get("version"),
        },
        "prompt_preview": (prompt := decrypted_doc.get("system_prompt", ""))[:300] + ("..." if len(prompt) > 300 else ""),
        "encryption_metadata": {
            "encryption_type": "Queryable Encryption (automatic)",
            "kms_provider": "AWS KMS (production) / local (dev)",
            "queryable_encrypted_fields": ["agent_name (equality)"],
            "non_queryable_encrypted_fields": ["system_prompt", "tool_config"],
            "collection": AGENT_PROFILES_COLLECTION,
            "database": DATABASE_NAME,
        },
    }