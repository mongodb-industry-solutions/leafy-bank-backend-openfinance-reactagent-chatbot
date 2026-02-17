from fastapi import APIRouter, HTTPException
import logging

from graph import db
from config import (
    DATABASE_NAME,
    CHECKPOINTS_AIO_COLLECTION,
    CHECKPOINTS_WRITES_AIO_COLLECTION,
)

logger = logging.getLogger(__name__)

# Create the router
router = APIRouter(prefix="/checkpointer", tags=["Checkpointer"])


@router.post("/clear-all-memory")
async def clear_all_memory():
    """Clear all checkpointer memory (all threads)."""
    try:
        deleted_count = 0
        result = db.db[CHECKPOINTS_AIO_COLLECTION].delete_many({})
        deleted_count += result.deleted_count
        result = db.db[CHECKPOINTS_WRITES_AIO_COLLECTION].delete_many({})
        deleted_count += result.deleted_count
        return {
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Deleted {deleted_count} memory records."
        }
    except Exception as e:
        logger.error(f"Error clearing memory: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
