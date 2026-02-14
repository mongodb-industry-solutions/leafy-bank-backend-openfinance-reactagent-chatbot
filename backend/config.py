import os
from dotenv import load_dotenv

load_dotenv()

# MongoDB
MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME")
CHECKPOINTS_AIO_COLLECTION = os.getenv("CHECKPOINTS_AIO_COLLECTION", "checkpoints_aio")
CHECKPOINTS_WRITES_AIO_COLLECTION = os.getenv("CHECKPOINTS_WRITES_AIO_COLLECTION", "checkpoint_writes_aio")

# AWS Bedrock
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
CHAT_COMPLETIONS_MODEL_ID = os.getenv("CHAT_COMPLETIONS_MODEL_ID")

# Open Finance Backend
OPEN_FINANCE_API_URL = os.getenv("OPEN_FINANCE_API_URL", "http://localhost:8003")
OPEN_FINANCE_API_BASE = f"{OPEN_FINANCE_API_URL}/api/v1"
