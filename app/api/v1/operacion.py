"""Counter operation: search, check-in, state change and check-out.

RF-019, RF-021 and RF-024. These routes share the ``/reservas`` prefix with
``reservas.py``; ``/reservas/buscar`` is declared in this module and the
aggregator includes it FIRST so the literal path is matched before
``/reservas/{reserva_id}``.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.deps import get_db, permisos_actuales, requiere_permiso
from app.models import Usuario
from app.schemas import CambioEstadoIn, CheckInIn, CheckOutIn, ErrorBody, Lista, ReservaOut
from app.services import operacion_service, reserva_service
from app.services.ensamblador import armar_reserva, armar_reservas

router = APIRouter(prefix="/reservas", tags=["operación"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.get(
    "/buscar",
    response_model=Lista[ReservaOut],
    responses=RESPUESTAS,
    summary="Buscar una reserva activa por código o placa",
)
def buscar(
    codigo: str | None = Query(default=None, description="Código AQL-XXXXXX."),
    placa: str | None = Query(default=None, description="Placa del vehículo."),
    _: Usuario = Depends(requiere_permiso("reserva:check_in")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> Lista[ReservaOut]:
    reservas = operacion_service.buscar(db, codigo=codigo, placa=placa)
    return Lista[ReservaOut](items=armar_reservas(db, reservas, permisos))


@router.post(
    "/{reserva_id}/check-in",
    response_model=ReservaOut,
    responses=RESPUESTAS,
    summary="Registrar el ingreso del vehículo",
)
def check_in(
    reserva_id: int,
    datos: CheckInIn,
    autor: Usuario = Depends(requiere_permiso("reserva:check_in")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ReservaOut:
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    reserva = operacion_service.check_in(db, reserva, datos, autor, permisos)
    return armar_reserva(db, reserva, permisos)


@router.post(
    "/{reserva_id}/estado",
    response_model=ReservaOut,
    responses=RESPUESTAS,
    summary="Avanzar el estado del servicio",
)
def cambiar_estado(
    reserva_id: int,
    datos: CambioEstadoIn,
    autor: Usuario = Depends(requiere_permiso("reserva:avanzar_estado")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ReservaOut:
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    reserva = operacion_service.cambiar_estado(db, reserva, datos.estado, autor, permisos)
    return armar_reserva(db, reserva, permisos)


@router.post(
    "/{reserva_id}/check-out",
    response_model=ReservaOut,
    responses=RESPUESTAS,
    summary="Registrar la entrega del vehículo",
)
def check_out(
    reserva_id: int,
    datos: CheckOutIn,
    autor: Usuario = Depends(requiere_permiso("reserva:check_out")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ReservaOut:
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    reserva = operacion_service.check_out(db, reserva, datos, autor, permisos)
    return armar_reserva(db, reserva, permisos)
