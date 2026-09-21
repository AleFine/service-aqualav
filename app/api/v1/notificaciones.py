"""The customer's notifications and their devices (RF-029, RF-022).

No permission gate on any of these: like ``GET /perfil`` and ``GET /estados``,
they only ever read or write the caller's OWN rows. A permission code exists to
separate what different people may do to OTHERS' data, and there is exactly one
person entitled to this one.
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.deps import get_db, usuario_actual
from app.models import Usuario
from app.repositories import notificacion as notificacion_repo
from app.schemas import (
    DispositivoIn,
    DispositivoOut,
    ErrorBody,
    Lista,
    NotificacionOut,
)
from app.services import notificacion_service
from app.services.ensamblador import armar_dispositivo, armar_notificacion

router = APIRouter(prefix="/notificaciones", tags=["notificaciones"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    404: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.get(
    "",
    response_model=Lista[NotificacionOut],
    responses=RESPUESTAS,
    summary="Bandeja de notificaciones propia",
)
def listar(
    limite: int = Query(
        default=notificacion_repo.LIMITE_BANDEJA,
        ge=1,
        le=notificacion_repo.LIMITE_BANDEJA,
        description="Cuántas notificaciones devolver, de la más reciente a la más antigua.",
    ),
    usuario: Usuario = Depends(usuario_actual),
    db: Session = Depends(get_db),
) -> Lista[NotificacionOut]:
    """RF-029: qué se envió, por qué canal y cómo terminó cada envío."""
    filas = notificacion_service.bandeja(db, usuario, limite=limite)
    return Lista[NotificacionOut](items=[armar_notificacion(fila) for fila in filas])


@router.get(
    "/dispositivos",
    response_model=Lista[DispositivoOut],
    responses=RESPUESTAS,
    summary="Dispositivos autorizados para recibir push",
)
def listar_dispositivos(
    usuario: Usuario = Depends(usuario_actual),
    db: Session = Depends(get_db),
) -> Lista[DispositivoOut]:
    filas = notificacion_service.listar_dispositivos(db, usuario)
    return Lista[DispositivoOut](items=[armar_dispositivo(fila) for fila in filas])


@router.post(
    "/dispositivos",
    response_model=DispositivoOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Registrar un dispositivo para recibir notificaciones push",
)
def registrar_dispositivo(
    datos: DispositivoIn,
    usuario: Usuario = Depends(usuario_actual),
    db: Session = Depends(get_db),
) -> DispositivoOut:
    """RF-029 precondición: el cliente autorizó las notificaciones push."""
    return armar_dispositivo(notificacion_service.registrar_dispositivo(db, usuario, datos))


@router.delete(
    "/dispositivos/{dispositivo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=RESPUESTAS,
    summary="Dejar de enviar push a un dispositivo",
)
def dar_de_baja_dispositivo(
    dispositivo_id: int,
    usuario: Usuario = Depends(usuario_actual),
    db: Session = Depends(get_db),
) -> None:
    notificacion_service.dar_de_baja_dispositivo(db, usuario, dispositivo_id)
