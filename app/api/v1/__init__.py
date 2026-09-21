"""Version 1 of the API. Every resource lives under ``/api/v1``."""

from fastapi import APIRouter

from app.api.v1 import (
    admin_bahias,
    admin_roles,
    admin_servicios,
    admin_tarifas,
    admin_usuarios,
    agenda,
    asignacion,
    auth,
    catalogo,
    disponibilidad,
    estados,
    health,
    interno,
    notificaciones,
    operacion,
    pagos,
    perfil,
    reembolsos,
    reservas,
    servicios,
    vehiculos,
)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(perfil.router)
api_router.include_router(vehiculos.router)
api_router.include_router(servicios.router)
api_router.include_router(catalogo.router)
api_router.include_router(admin_servicios.router)
api_router.include_router(admin_tarifas.router)
api_router.include_router(admin_roles.router)
api_router.include_router(admin_usuarios.router)
api_router.include_router(admin_bahias.router)
api_router.include_router(agenda.router)
api_router.include_router(disponibilidad.router)
api_router.include_router(estados.router)
api_router.include_router(notificaciones.router)
api_router.include_router(interno.router)
# ``operacion`` and ``asignacion`` first: they own the literal
# ``/reservas/buscar``, ``/reservas/atencion-inmediata`` and ``/reservas/cola``,
# which would otherwise be swallowed by ``/reservas/{reserva_id}``.
api_router.include_router(operacion.router)
api_router.include_router(asignacion.router)
api_router.include_router(pagos.router)
# RF-027 CA-02: the receipt PDF hangs off the receipt, not off the reservation.
api_router.include_router(pagos.comprobantes)
api_router.include_router(reembolsos.router)
api_router.include_router(reservas.router)

__all__ = ["api_router"]
