"""
Encrypted MongoDB connector for Queryable Encryption on agent profiles.

Supports two KMS providers:
  - "aws" (production/Kanopy) — uses AWS KMS via IRSA, no local key needed
  - "local" (dev fallback) — reads master-key.bin from disk

Set KMS_PROVIDER=aws and AWS_KMS_KEY_ARN in environment for production.
"""

import logging
from pathlib import Path

from bson import json_util
from pymongo import MongoClient
from pymongo.encryption_options import AutoEncryptionOpts

from config import (
    MONGODB_URI,
    DATABASE_NAME,
    APP_NAME,
    ENCRYPTION_CONFIG_PATH,
    KMS_PROVIDER,
    CRYPT_SHARED_LIB_PATH,
    LOCAL_MASTER_KEY_PATH,
)
from agent.db.mdb import MongoDBConnector

logger = logging.getLogger(__name__)


class EncryptedMongoDBConnector(MongoDBConnector):
    """MongoDBConnector backed by an encrypted MongoClient.

    Behaves identically to MongoDBConnector for non-encrypted collections.
    Collections listed in the encrypted_fields_map get automatic
    encryption/decryption via the PyMongo driver.
    """

    def __init__(self, auto_encryption_opts, uri=None, database_name=None, appname=None):
        self.uri = uri or MONGODB_URI
        self.database_name = database_name or DATABASE_NAME
        self.appname = appname or APP_NAME
        self.client = MongoClient(
            self.uri, appname=self.appname, auto_encryption_opts=auto_encryption_opts
        )
        self.db = self.client[self.database_name]


def load_encryption_config(config_path: str) -> dict:
    """Load the encryption config (encrypted_fields_map with keyIds) from JSON.

    Uses bson.json_util to deserialize BSON Binary UUIDs that standard json can't handle.
    """
    with open(config_path) as f:
        raw = f.read()
    return json_util.loads(raw)


def _build_kms_providers() -> dict:
    """Build KMS providers based on environment.

    KMS_PROVIDER=aws  -> AWS KMS (Kanopy/production). Credentials auto-discovered
                         via IRSA (pymongo-auth-aws handles STS AssumeRoleWithWebIdentity).
    KMS_PROVIDER=local (default) -> Local 96-byte master key file for dev.
    """
    if KMS_PROVIDER == "aws":
        return {"aws": {}}

    master_key_path = LOCAL_MASTER_KEY_PATH or str(
        Path(__file__).resolve().parent.parent.parent / "master-key.bin"
    )
    master_key = Path(master_key_path).read_bytes()
    return {"local": {"key": master_key}}


def create_encrypted_connector(config: dict) -> EncryptedMongoDBConnector:
    """Create an EncryptedMongoDBConnector from encryption config.

    Args:
        config: Dict from load_encryption_config() containing
                key_vault_namespace and encrypted_fields_map.

    Returns:
        EncryptedMongoDBConnector ready for use.
    """
    kms_providers = _build_kms_providers()

    crypt_shared_lib_path = CRYPT_SHARED_LIB_PATH or str(
        Path(__file__).resolve().parent.parent.parent / "lib" / "mongo_crypt_v1.dylib"
    )

    auto_opts = AutoEncryptionOpts(
        kms_providers,
        config["key_vault_namespace"],
        encrypted_fields_map=config["encrypted_fields_map"],
        crypt_shared_lib_path=crypt_shared_lib_path,
        crypt_shared_lib_required=True,
    )

    return EncryptedMongoDBConnector(auto_opts)


def get_encrypted_connector() -> EncryptedMongoDBConnector:
    """Create an EncryptedMongoDBConnector using the config from environment or local fallback.

    Resolution order for encryption_config.json:
      1. ENCRYPTION_CONFIG_PATH env var (Kanopy: /etc/encryption/encryption_config.json)
      2. backend/encryption_config.json (local dev)

    Raises RuntimeError if config file not found.
    """
    config_path = ENCRYPTION_CONFIG_PATH or str(
        Path(__file__).resolve().parent.parent.parent / "encryption_config.json"
    )
    if not Path(config_path).exists():
        raise RuntimeError(
            f"encryption_config.json not found at {config_path}. "
            "Run: cd backend && poetry run python ../scripts/setup_encrypted_profiles.py"
        )
    logger.info(f"Loading encryption config from {config_path}")
    config = load_encryption_config(config_path)
    return create_encrypted_connector(config)
