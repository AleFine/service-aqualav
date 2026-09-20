"""Bay and operator assignment (RF-020).

Shares the ``/reservas`` prefix with ``reservas.py``; the aggregator includes
this module FIRST so the literal ``/reservas/cola`` is matched before
``/reservas/{reserva_id}``.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_db, permisos_actuales, requiere_permiso
from app.models import Usuario
from app.schemas import AsignacionIn, ErrorBody, Lista, ReservaOut, ResultadoAsignacionOut
from app.services import asignacion_service, reserva_service
from app.services.ensamblador import armar_reservas, armar_resultado_asignacion

router = APIRouter(prefix="/reservas", tags=["operación"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.get(
    "/cola",
    response_model=Lista[ReservaOut],
    responses=RESPUESTAS,
    summary="Cola de servicios asignados al operario que consulta",
)
def cola(
    operario: Usuario = Depends(requiere_permiso(asignacion_service.PERMISO_OPERAR)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> Lista[ReservaOut]:
    reservas = asignacion_service.cola_del_operario(db, operario)
    return Lista[ReservaOut](items=armar_reservas(db, reservas, permisos))


@router.post(
    "/{reserva_id}/asignacion",
    response_model=ResultadoAsignacionOut,
    responses=RESPUESTAS,
    summary="Asignar bahía y operario, o dejar el servicio en cola",
)
def asignar(
    reserva_id: int,
    datos: AsignacionIn,
    autor: Usuario = Depends(requiere_permiso(asignacion_service.PERMISO_ASIGNAR)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ResultadoAsignacionOut:
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    resultado = asignacion_service.asignar(db, reserva, datos, autor, permisos)
    return armar_resultado_asignacion(db, resultado, permisos)
