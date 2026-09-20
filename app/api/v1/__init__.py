"""Version 1 of the API. Every resource lives under ``/api/v1``."""

from fastapi import APIRouter

from app.api.v1 import (
    admin_roles,
    admin_servicios,
    auth,
    disponibilidad,
    estados,
    health,
    operacion,
    pagos,
    reservas,
    servicios,
    vehiculos,
)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(vehiculos.router)
api_router.include_router(servicios.router)
api_router.include_router(admin_servicios.router)
api_router.include_router(admin_roles.router)
api_router.include_router(disponibilidad.router)
api_router.include_router(estados.router)
# ``operacion`` first: it owns the literal ``/reservas/buscar``, which would
# otherwise be swallowed by ``/reservas/{reserva_id}``.
api_router.include_router(operacion.router)
api_router.include_router(pagos.router)
api_router.include_router(reservas.router)

__all__ = ["api_router"]
