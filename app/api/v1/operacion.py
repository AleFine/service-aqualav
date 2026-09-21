"""Counter operation: search, check-in, state change, review and check-out.

RF-019, RF-021 and RF-024. These routes share the ``/reservas`` prefix with
``reservas.py``; ``/reservas/buscar`` and ``/reservas/atencion-inmediata`` are
declared in this module and the aggregator includes it FIRST so the literal
paths are matched before ``/reservas/{reserva_id}``.
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.deps import get_db, permisos_actuales, requiere_algun_permiso, requiere_permiso
from app.models import Usuario
from app.schemas import (
    AtencionInmediataIn,
    CambioEstadoIn,
    CheckInIn,
    CheckOutIn,
    ErrorBody,
    Lista,
    RecordatorioRespuestaIn,
    ReservaOut,
    RespuestaRecordatorioOut,
    RevisionIn,
)
from app.services import operacion_service, recordatorio_service, reserva_service
from app.services.ensamblador import armar_recordatorio, armar_reserva, armar_reservas

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
    qr: str | None = Query(default=None, description="Token leído del código QR."),
    _: Usuario = Depends(requiere_permiso("reserva:check_in")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> Lista[ReservaOut]:
    reservas = operacion_service.buscar(db, codigo=codigo, placa=placa, qr=qr)
    return Lista[ReservaOut](items=armar_reservas(db, reservas, permisos))


@router.post(
    "/atencion-inmediata",
    response_model=ReservaOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Abrir una atención para un cliente que llegó sin reserva",
)
def atencion_inmediata(
    datos: AtencionInmediataIn,
    autor: Usuario = Depends(requiere_permiso("reserva:check_in")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ReservaOut:
    reserva = reserva_service.atencion_inmediata(db, datos, autor, permisos)
    return armar_reserva(db, reserva, permisos)


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
    "/{reserva_id}/revision",
    response_model=ReservaOut,
    responses=RESPUESTAS,
    summary="Registrar la observación del cliente y enviar el servicio a revisión",
)
def revision(
    reserva_id: int,
    datos: RevisionIn,
    autor: Usuario = Depends(requiere_permiso("reserva:revisar")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ReservaOut:
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    reserva = operacion_service.enviar_a_revision(db, reserva, datos, autor, permisos)
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


@router.post(
    "/{reserva_id}/recordatorio",
    response_model=RespuestaRecordatorioOut,
    responses=RESPUESTAS,
    summary="Responder al recordatorio: confirmar asistencia, reprogramar o cancelar",
)
def responder_recordatorio(
    reserva_id: int,
    datos: RecordatorioRespuestaIn,
    autor: Usuario = Depends(requiere_algun_permiso("reserva:leer_propias", "reserva:leer_todas")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> RespuestaRecordatorioOut:
    """RF-030. Cancelar exige además ``reserva:cancelar``: la valida la transición."""
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    reserva, recordatorio = recordatorio_service.responder(db, reserva, datos, autor, permisos)
    return RespuestaRecordatorioOut(
        reserva=armar_reserva(db, reserva, permisos),
        recordatorio=armar_recordatorio(recordatorio),
    )
