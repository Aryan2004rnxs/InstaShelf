from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
async def health_check():
    """Health check endpoint for UptimeRobot monitoring."""
    return {"status": "ok"}
