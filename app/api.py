"""HTTP routes."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health", tags=["operations"], summary="Liveness check")
def health() -> dict[str, str]:
    return {"status": "ok"}
