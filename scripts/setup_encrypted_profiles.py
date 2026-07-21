"""
One-time setup: Create the encrypted agent profiles collection with Queryable Encryption.

Creates the `openbankingAgentProfiles` collection in the DATABASE_NAME database,
generates data encryption keys (DEKs) for each encrypted field, creates indexes,
and saves the resulting encrypted_fields_map to encryption_config.json for runtime use.

Reuses the same master key as consent QE (open-finance-next-gen/backend/master-key.bin).
Copy it to backend/master-key.bin or set LOCAL_MASTER_KEY_PATH to its location.

Supports two KMS providers:
  KMS_PROVIDER=local (default) — uses master-key.bin for dev
  KMS_PROVIDER=aws             — uses AWS KMS (requires AWS_KMS_KEY_ARN)

Usage:
    cd backend && poetry run python ../scripts/setup_encrypted_profiles.py
"""

import os
import sys
from pathlib import Path

from bson import json_util
from bson.codec_options import CodecOptions
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.encryption import ClientEncryption
from pymongo.encryption_options import AutoEncryptionOpts

# Load .env from backend/
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
load_dotenv(BACKEND_DIR / ".env")

MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    print("ERROR: MONGODB_URI not set in backend/.env")
    sys.exit(1)

DB_NAME = os.getenv("DATABASE_NAME", "leafy_bank_bian")
COLL_NAME = "openbankingAgentProfiles"
KEY_VAULT_NAMESPACE = "encryption.__keyVault_agent_profiles"
MASTER_KEY_PATH = os.getenv(
    "LOCAL_MASTER_KEY_PATH",
    str(BACKEND_DIR / "master-key.bin"),
)
CRYPT_SHARED_LIB_PATH = os.getenv(
    "CRYPT_SHARED_LIB_PATH",
    str(BACKEND_DIR / "lib" / "mongo_crypt_v1.dylib"),
)
CONFIG_OUTPUT_PATH = BACKEND_DIR / "encryption_config.json"

# 3 encrypted fields: agent_name (queryable), system_prompt, tool_config
ENCRYPTED_FIELDS = {
    "fields": [
        {
            "path": "agent_name",
            "bsonType": "string",
            "keyId": None,
            "queries": [{"queryType": "equality"}],
        },
        {
            "path": "system_prompt",
            "bsonType": "string",
            "keyId": None,
        },
        {
            "path": "tool_config",
            "bsonType": "object",
            "keyId": None,
        },
    ]
}

KMS_PROVIDER = os.getenv("KMS_PROVIDER", "local")
AWS_KMS_KEY_ARN = os.getenv("AWS_KMS_KEY_ARN")
AWS_KMS_REGION = os.getenv("AWS_KMS_REGION", "us-east-1")


def load_master_key() -> bytes:
    """Load the existing master key from file (shared with consent QE)."""
    key_path = Path(MASTER_KEY_PATH)
    if not key_path.exists():
        print(f"ERROR: Master key not found at {key_path}")
        print("Copy it from open-finance-next-gen/backend/master-key.bin:")
        print(f"  cp ../open-finance-next-gen/backend/master-key.bin {BACKEND_DIR}/master-key.bin")
        print("Or set LOCAL_MASTER_KEY_PATH to point to the existing key.")
        sys.exit(1)
    key = key_path.read_bytes()
    if len(key) != 96:
        print(f"ERROR: Master key must be 96 bytes, got {len(key)}")
        sys.exit(1)
    print(f"Loaded master key from {key_path}")
    return key


def build_kms_config() -> tuple[dict, str, dict | None]:
    """Build KMS providers, provider name, and master_key arg."""
    if KMS_PROVIDER == "aws":
        if not AWS_KMS_KEY_ARN:
            print("ERROR: KMS_PROVIDER=aws but AWS_KMS_KEY_ARN not set")
            sys.exit(1)
        print(f"Using AWS KMS: {AWS_KMS_KEY_ARN}")
        kms_providers = {"aws": {}}
        master_key_arg = {"key": AWS_KMS_KEY_ARN, "region": AWS_KMS_REGION}
        return kms_providers, "aws", master_key_arg

    master_key = load_master_key()
    return {"local": {"key": master_key}}, "local", None


def main():
    print("=" * 60)
    print("Setup: Encrypted Agent Profiles Collection")
    print(f"KMS Provider: {KMS_PROVIDER}")
    print(f"Database: {DB_NAME}")
    print(f"Collection: {COLL_NAME}")
    print("=" * 60)

    # Step 1: Build KMS config
    kms_providers, kms_provider_name, master_key_arg = build_kms_config()

    # Step 2: Key vault setup
    key_vault_client = MongoClient(MONGODB_URI)
    kv_db, kv_coll = KEY_VAULT_NAMESPACE.split(".", 1)
    key_vault = key_vault_client[kv_db][kv_coll]
    key_vault.create_index(
        "keyAltNames",
        unique=True,
        partialFilterExpression={"keyAltNames": {"$exists": True}},
    )
    print(f"Key vault ready: {KEY_VAULT_NAMESPACE}")

    # Step 3: Create encrypted collection
    client_encryption = ClientEncryption(
        kms_providers,
        KEY_VAULT_NAMESPACE,
        key_vault_client,
        CodecOptions(),
    )

    db = key_vault_client[DB_NAME]

    # Drop existing collection if it exists (idempotent re-runs)
    existing_colls = db.list_collection_names()
    if COLL_NAME in existing_colls:
        print(f"Dropping existing collection: {DB_NAME}.{COLL_NAME}")
        db.drop_collection(COLL_NAME)

    # Also drop associated QE metadata collections
    for suffix in ["esc", "ecoc"]:
        meta_coll = f"enxcol_.{COLL_NAME}.{suffix}"
        if meta_coll in existing_colls:
            print(f"Dropping metadata collection: {meta_coll}")
            db.drop_collection(meta_coll)

    print(f"\nCreating encrypted collection: {DB_NAME}.{COLL_NAME}")
    create_kwargs = {"kms_provider": kms_provider_name}
    if master_key_arg:
        create_kwargs["master_key"] = master_key_arg
    _, ef_map = client_encryption.create_encrypted_collection(
        db, COLL_NAME, ENCRYPTED_FIELDS, **create_kwargs
    )
    print("Encrypted collection created with auto-generated data keys.")

    # Step 4: Create indexes on plaintext fields
    encrypted_coll_plain = key_vault_client[DB_NAME][COLL_NAME]
    encrypted_coll_plain.create_index("is_active")
    print("Indexes created: is_active")

    # Step 5: Save encryption config for runtime
    config = {
        "key_vault_namespace": KEY_VAULT_NAMESPACE,
        "encrypted_fields_map": {
            f"{DB_NAME}.{COLL_NAME}": ef_map,
        },
    }

    config_json = json_util.dumps(config, indent=2)
    CONFIG_OUTPUT_PATH.write_text(config_json)
    print(f"\nEncryption config saved to: {CONFIG_OUTPUT_PATH}")

    # Step 6: Verify config round-trip
    print("\nVerifying config round-trip...")
    loaded = json_util.loads(CONFIG_OUTPUT_PATH.read_text())
    ef_key = f"{DB_NAME}.{COLL_NAME}"
    loaded_fields = loaded["encrypted_fields_map"][ef_key]["fields"]
    print(f"  Fields in config: {len(loaded_fields)}")
    for f in loaded_fields:
        key_id_type = type(f["keyId"]).__name__
        queryable = "equality" if f.get("queries") else "no"
        print(f"  - {f['path']} ({f['bsonType']}, keyId: {key_id_type}, queryable: {queryable})")

    # Step 7: Quick insert/query test
    print("\nRunning quick insert/query test...")
    auto_opts = AutoEncryptionOpts(
        kms_providers,
        KEY_VAULT_NAMESPACE,
        encrypted_fields_map={ef_key: ef_map},
        crypt_shared_lib_path=CRYPT_SHARED_LIB_PATH,
    )
    encrypted_client = MongoClient(MONGODB_URI, auto_encryption_opts=auto_opts)
    encrypted_coll = encrypted_client[DB_NAME][COLL_NAME]

    test_doc = {
        "agent_name": "__test_agent__",
        "profile_name": "test",
        "is_active": False,
        "system_prompt": "You are a test agent. This prompt should be encrypted.",
        "tool_config": {"tools": ["test_tool_1", "test_tool_2"]},
        "version": 1,
        "metadata": {"description": "Setup verification test"},
    }

    encrypted_coll.insert_one(test_doc)
    result = encrypted_coll.find_one({"agent_name": "__test_agent__"})
    if result and result["agent_name"] == "__test_agent__":
        print("  Insert + equality query on encrypted agent_name: OK")
    else:
        print("  ERROR: Equality query on encrypted agent_name failed!")
        sys.exit(1)

    if result["system_prompt"] == test_doc["system_prompt"]:
        print("  Decrypted system_prompt matches: OK")
    else:
        print("  ERROR: system_prompt decryption mismatch!")
        sys.exit(1)

    # Verify encryption on disk
    raw = key_vault_client[DB_NAME][COLL_NAME].find_one({"is_active": False, "profile_name": "test"})
    raw_prompt_type = type(raw.get("system_prompt")).__name__
    raw_name_type = type(raw.get("agent_name")).__name__
    raw_config_type = type(raw.get("tool_config")).__name__

    if raw_prompt_type == "Binary":
        print(f"  Encryption on disk verified: system_prompt is Binary")
    else:
        print(f"  WARNING: system_prompt is {raw_prompt_type}, expected Binary")

    if raw_name_type == "Binary":
        print(f"  Encryption on disk verified: agent_name is Binary")
    else:
        print(f"  WARNING: agent_name is {raw_name_type}, expected Binary")

    if raw_config_type == "Binary":
        print(f"  Encryption on disk verified: tool_config is Binary")
    else:
        print(f"  WARNING: tool_config is {raw_config_type}, expected Binary")

    # Clean up test doc
    encrypted_coll.delete_one({"agent_name": "__test_agent__"})
    print("  Test document cleaned up.")

    # Cleanup clients
    client_encryption.close()
    encrypted_client.close()
    key_vault_client.close()

    print("\n" + "=" * 60)
    print("Setup complete!")
    print(f"  Collection: {DB_NAME}.{COLL_NAME}")
    print(f"  Config: {CONFIG_OUTPUT_PATH}")
    print(f"  Encrypted fields: {len(ENCRYPTED_FIELDS['fields'])}")
    print(f"  Queryable fields: agent_name (equality)")
    print("=" * 60)


if __name__ == "__main__":
    main()
