"""Agent profile service — reads/writes agent prompts from QE-encrypted MongoDB collection."""

import logging
from datetime import UTC, datetime
from pathlib import Path

from config import AGENT_PROFILES_COLLECTION
from agent.db.encrypted_connector import EncryptedMongoDBConnector

logger = logging.getLogger(__name__)

# Agent name → prompt file path (relative to backend/agent/prompts/)
PROMPT_FILES = {
    "consent_agent": "consent.md",
    "financial_advice_agent": "financial_advice.md",
    "supervisor": "supervisor.md",
}

# Agent name → tool names (for seed data; actual tool binding stays in code)
AGENT_TOOLS = {
    "consent_agent": {
        "tools": [
            "list_institutions",
            "get_default_permissions",
            "create_consent",
            "get_consent",
            "list_user_consents",
            "request_bank_login",
            "approve_consent",
            "revoke_consent",
            "fetch_and_cache_data",
        ]
    },
    "financial_advice_agent": {
        "tools": [
            "get_current_user_id",
            "find",
            "aggregate",
            "count",
            "list-collections",
            "collection-schema",
        ]
    },
    "supervisor": {
        "tools": [],
        "routes_to": ["consent_agent", "financial_advice_agent", "FINISH"],
    },
}

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class AgentProfileService:
    """Manages agent profiles in an encrypted MongoDB collection.

    All reads/writes go through the encrypted connector — system_prompt,
    tool_config, and agent_name are automatically encrypted/decrypted
    by the PyMongo driver.
    """

    def __init__(self, encrypted_db: EncryptedMongoDBConnector):
        self.collection = encrypted_db.get_collection(AGENT_PROFILES_COLLECTION)
        self._ensure_indexes()

    def _ensure_indexes(self):
        """Create indexes on plaintext fields. agent_name is encrypted — QE handles its queries."""
        self.collection.create_index("is_active")
        logger.info("AgentProfileService indexes ensured")

    def _seed_one(self, agent_name: str, filename: str) -> int:
        """Insert a default (active) profile for one agent from its .md file.

        Returns the system_prompt length. Raises if the file is missing.
        """
        prompt_path = PROMPTS_DIR / filename
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

        system_prompt = prompt_path.read_text()
        now = datetime.now(UTC)
        doc = {
            "agent_name": agent_name,
            "profile_name": "default",
            "is_active": True,
            "system_prompt": system_prompt,
            "tool_config": AGENT_TOOLS.get(agent_name, {"tools": []}),
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "metadata": {
                "description": f"Default profile seeded from {filename}",
                "author": "system",
            },
        }
        self.collection.insert_one(doc)
        return len(system_prompt)

    def seed_from_files(self):
        """Seed default profiles from .md files for any agent missing one.

        Idempotent per-agent: an agent with no active profile is seeded from
        its .md file; agents that already have an active profile are left
        untouched. Safe to call on every startup, and self-heals when a new
        agent is added (e.g. after a rename) without wiping existing prompts.
        """
        seeded = []
        for agent_name, filename in PROMPT_FILES.items():
            if self.collection.find_one({"agent_name": agent_name, "is_active": True}):
                continue
            chars = self._seed_one(agent_name, filename)
            seeded.append(agent_name)
            logger.info(f"  Seeded: {agent_name} ({chars} chars)")

        if seeded:
            logger.info(f"Seeded {len(seeded)} agent profile(s): {seeded}")
        else:
            logger.info("All agent profiles already present — nothing to seed")

    def sync_from_files(self) -> dict[str, str]:
        """Update each active profile's system_prompt from the .md files on disk.

        Agents that have no active profile yet (e.g. one added since the last
        seed, or a rename) are seeded from their file instead of skipped.
        Uses update_one by _id (bypasses QE equality query limitations on find).
        Returns {agent_name: status} summary.
        """
        results = {}
        for agent_name, filename in PROMPT_FILES.items():
            prompt_path = PROMPTS_DIR / filename
            if not prompt_path.exists():
                results[agent_name] = "file_not_found"
                continue

            new_prompt = prompt_path.read_text()
            active = self.collection.find_one(
                {"agent_name": agent_name, "is_active": True}
            )
            if not active:
                # New agent (e.g. after a rename) — seed it rather than skip,
                # so /reload can introduce agents added since the last seed.
                chars = self._seed_one(agent_name, filename)
                results[agent_name] = f"seeded ({chars} chars)"
                continue

            current_version = active.get("version", 0)
            result = self.collection.update_one(
                {"_id": active["_id"], "version": current_version},
                {"$set": {
                    "system_prompt": new_prompt,
                    "version": current_version + 1,
                    "updated_at": datetime.now(UTC),
                }},
            )
            if result.modified_count:
                results[agent_name] = f"updated ({len(new_prompt)} chars)"
            else:
                results[agent_name] = "unchanged"

        logger.info(f"Synced agent profiles from files: {results}")
        return results

    def get_active_prompts(self) -> dict[str, str]:
        """Return {agent_name: system_prompt} for all active profiles.

        Raises ValueError if any of the 4 expected agents is missing.
        """
        docs = list(self.collection.find({"is_active": True}))
        prompts = {doc["agent_name"]: doc["system_prompt"] for doc in docs}

        missing = set(PROMPT_FILES.keys()) - set(prompts.keys())
        if missing:
            raise ValueError(
                f"Missing active profiles for: {', '.join(missing)}. "
                "Run seed_from_files() or create profiles via admin API."
            )

        logger.info(f"Loaded {len(prompts)} active agent profiles from MongoDB")
        return prompts

    def get_active_profile(self, agent_name: str) -> dict | None:
        """Get the full active profile document for an agent."""
        return self.collection.find_one({"agent_name": agent_name, "is_active": True})

    def list_profiles(self, agent_name: str | None = None) -> list[dict]:
        """List all profiles, optionally filtered by agent_name."""
        query = {"agent_name": agent_name} if agent_name else {}
        return list(self.collection.find(query))

    def create_profile(self, agent_name: str, profile_name: str,
                       system_prompt: str, tool_config: dict,
                       metadata: dict | None = None) -> str:
        """Create a new profile (inactive by default).

        Checks for duplicates before inserting since QE doesn't support
        unique indexes on encrypted fields.
        """
        existing = self.collection.find_one(
            {"agent_name": agent_name, "profile_name": profile_name}
        )
        if existing:
            raise ValueError(f"Profile already exists: {agent_name}/{profile_name}")

        now = datetime.now(UTC)
        doc = {
            "agent_name": agent_name,
            "profile_name": profile_name,
            "is_active": False,
            "system_prompt": system_prompt,
            "tool_config": tool_config,
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "metadata": metadata or {},
        }
        result = self.collection.insert_one(doc)
        logger.info(f"Created profile: {agent_name}/{profile_name}")
        return str(result.inserted_id)

    def update_profile(self, agent_name: str, profile_name: str,
                       system_prompt: str | None = None,
                       tool_config: dict | None = None,
                       metadata: dict | None = None) -> int:
        """Update an existing profile's prompt and/or metadata. Bumps version.

        Uses read-then-write for version bump because QE collections
        don't reliably support $inc alongside $set on encrypted fields.
        """
        current = self.collection.find_one(
            {"agent_name": agent_name, "profile_name": profile_name}
        )
        if not current:
            return 0

        current_version = current.get("version", 0)
        update_fields: dict = {
            "updated_at": datetime.now(UTC),
            "version": current_version + 1,
        }
        if system_prompt is not None:
            update_fields["system_prompt"] = system_prompt
        if tool_config is not None:
            update_fields["tool_config"] = tool_config
        if metadata is not None:
            update_fields["metadata"] = metadata

        # Optimistic lock: version in filter ensures no concurrent modification
        result = self.collection.update_one(
            {"_id": current["_id"], "version": current_version},
            {"$set": update_fields},
        )
        if result.modified_count == 0 and result.matched_count == 0:
            logger.warning(f"Profile {agent_name}/{profile_name} was modified concurrently — retry")
            return 0
        if result.modified_count:
            logger.info(f"Updated profile: {agent_name}/{profile_name}")
        return result.modified_count

    def activate_profile(self, agent_name: str, profile_name: str) -> bool:
        """Activate a profile and deactivate all others for that agent.

        Activates first, then deactivates others — if a crash occurs between
        the two steps, the worst case is two active profiles (harmless)
        rather than zero (agent unavailable).

        Uses individual update_one calls because QE doesn't support update_many.
        """
        # Activate the target first
        result = self.collection.update_one(
            {"agent_name": agent_name, "profile_name": profile_name},
            {"$set": {"is_active": True, "updated_at": datetime.now(UTC)}},
        )
        if not result.modified_count and not result.matched_count:
            logger.warning(f"Profile not found: {agent_name}/{profile_name}")
            return False

        # Deactivate all other profiles for this agent (QE doesn't support update_many)
        all_profiles = list(self.collection.find({"agent_name": agent_name}))
        now = datetime.now(UTC)
        for profile in all_profiles:
            if profile.get("profile_name") != profile_name:
                self.collection.update_one(
                    {"_id": profile["_id"]},
                    {"$set": {"is_active": False, "updated_at": now}},
                )

        logger.info(f"Activated profile: {agent_name}/{profile_name}")
        return True

    def delete_profile(self, agent_name: str, profile_name: str) -> bool:
        """Delete a profile. Prevents deleting the last or active profile for an agent."""
        target = self.collection.find_one(
            {"agent_name": agent_name, "profile_name": profile_name}
        )
        if not target:
            return False

        if target.get("is_active"):
            logger.warning(f"Cannot delete active profile {agent_name}/{profile_name} — deactivate first")
            return False

        # Use find instead of count_documents — more reliable on QE encrypted fields
        count = len(list(self.collection.find({"agent_name": agent_name}, {"_id": 1})))
        if count <= 1:
            logger.warning(f"Cannot delete last profile for {agent_name}")
            return False

        result = self.collection.delete_one({"_id": target["_id"]})
        if result.deleted_count:
            logger.info(f"Deleted profile: {agent_name}/{profile_name}")
            return True
        return False