import os
from dotenv import load_dotenv

load_dotenv()

# App
APP_NAME = os.getenv("APP_NAME")

# MongoDB
MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME")
LEAFY_BANK_MONGODB_URI = os.getenv("LEAFY_BANK_MONGODB_URI")
CHECKPOINTS_AIO_COLLECTION = os.getenv("CHECKPOINTS_AIO_COLLECTION", "checkpoints_aio")
CHECKPOINTS_WRITES_AIO_COLLECTION = os.getenv("CHECKPOINTS_WRITES_AIO_COLLECTION", "checkpoint_writes_aio")

# AWS Bedrock
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
CHAT_COMPLETIONS_MODEL_ID = os.getenv("CHAT_COMPLETIONS_MODEL_ID")

# Suggestions
SUGGESTIONS_MODEL_ID = os.getenv("SUGGESTIONS_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

# Queryable Encryption
AGENT_PROFILES_COLLECTION = os.getenv("AGENT_PROFILES_COLLECTION", "encrypted_agent_profiles")
ENCRYPTION_CONFIG_PATH = os.getenv("ENCRYPTION_CONFIG_PATH", "")
KMS_PROVIDER = os.getenv("KMS_PROVIDER", "local")
AWS_KMS_KEY_ARN = os.getenv("AWS_KMS_KEY_ARN", "")
CRYPT_SHARED_LIB_PATH = os.getenv("CRYPT_SHARED_LIB_PATH", "")
LOCAL_MASTER_KEY_PATH = os.getenv("LOCAL_MASTER_KEY_PATH", "")

# Open Finance Backend
OPEN_FINANCE_API_URL = os.getenv("OPEN_FINANCE_API_URL", "http://localhost:8003")
OPEN_FINANCE_API_BASE = f"{OPEN_FINANCE_API_URL}/api/v1"
