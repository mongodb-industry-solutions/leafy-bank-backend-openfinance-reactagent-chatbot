from fastapi import APIRouter, HTTPException
from pymongo import MongoClient
import logging
import os
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Create the router
router = APIRouter(prefix="/checkpointer", tags=["Checkpointer"])

# MongoDB config
MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME")
CHECKPOINTS_COLLECTION = os.getenv("CHECKPOINTS_AIO_COLLECTION", "checkpoints_aio")
CHECKPOINT_WRITES_COLLECTION = os.getenv("CHECKPOINTS_WRITES_AIO_COLLECTION", "checkpoint_writes_aio")

mongodb_client = MongoClient(MONGODB_URI)


@router.post("/clear-all-memory")
async def clear_all_memory():
    """Clear all checkpointer memory (all threads)."""
    try:
        deleted_count = 0
        result = mongodb_client[DATABASE_NAME][CHECKPOINTS_COLLECTION].delete_many({})
        deleted_count += result.deleted_count
        result = mongodb_client[DATABASE_NAME][CHECKPOINT_WRITES_COLLECTION].delete_many({})
        deleted_count += result.deleted_count
        return {
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Deleted {deleted_count} memory records."
        }
    except Exception as e:
        logger.error(f"Error clearing memory: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
