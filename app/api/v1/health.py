"""Liveness probe (RNF-010)."""

from fastapi import APIRouter

router = APIRouter(tags=["salud"])


@router.get("/health", summary="Estado del servicio")
def health() -> dict[str, str]:
    """Answer 200 as long as the process is able to serve requests."""
    return {"status": "ok"}
