"""Reservation endpoints (RF-014, RF-015, RF-016, RF-017, RF-022)."""

import math
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.deps import get_db, permisos_actuales, requiere_algun_permiso, requiere_permiso
from app.models import Usuario
from app.schemas import (
    TAMANIO_PAGINA_DEFECTO,
    TAMANIO_PAGINA_MAXIMO,
    CancelacionIn,
    ErrorBody,
    Pagina,
    ReprogramacionIn,
    ReservaCrear,
    ReservaOut,
)
from app.services import reserva_service
from app.services.ensamblador import armar_reserva, armar_reservas

router = APIRouter(prefix="/reservas", tags=["reservas"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}

LECTURA = ("reserva:leer_propias", "reserva:leer_todas")


@router.post(
    "",
    response_model=ReservaOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Confirmar una reserva",
)
def crear(
    datos: ReservaCrear,
    usuario: Usuario = Depends(requiere_permiso("reserva:crear")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ReservaOut:
    reserva = reserva_service.crear(db, usuario, datos)
    return armar_reserva(db, reserva, permisos)


@router.get(
    "",
    response_model=Pagina[ReservaOut],
    responses=RESPUESTAS,
    summary="Historial de reservas, paginado",
)
def listar(
    estado: Annotated[
        list[str] | None,
        Query(
            description=(
                "Filtro por estado. **Repetible**: `?estado=confirmada&estado=en_lavado` "
                "devuelve la unión de ambos, que es lo que necesitan las pantallas de "
                "agregado (RF-017 v1.0). Enviado una sola vez se comporta igual que antes."
            ),
        ),
    ] = None,
    vehiculo_id: int | None = Query(
        default=None, ge=1, description="Solo las reservas de ese vehículo (RF-017 `CA-02`)."
    ),
    desde: date | None = Query(
        default=None, description="Reservas que inician ese día o después (hora de Lima)."
    ),
    hasta: date | None = Query(
        default=None, description="Reservas que inician ese día o antes (hora de Lima)."
    ),
    pagina: int = Query(default=1, ge=1),
    tamanio: int = Query(default=TAMANIO_PAGINA_DEFECTO, ge=1, le=TAMANIO_PAGINA_MAXIMO),
    usuario: Usuario = Depends(requiere_algun_permiso(*LECTURA)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> Pagina[ReservaOut]:
    """RF-017 y su delta v1.0: filtros por estado (uno o varios), vehículo y rango."""
    items, total, pagina, tamanio = reserva_service.listar(
        db,
        usuario,
        permisos,
        estados=estado,
        vehiculo_id=vehiculo_id,
        desde=desde,
        hasta=hasta,
        pagina=pagina,
        tamanio=tamanio,
    )
    return Pagina[ReservaOut](
        items=armar_reservas(db, items, permisos),
        pagina=pagina,
        tamanio=tamanio,
        total=total,
        total_paginas=math.ceil(total / tamanio) if total else 0,
    )


@router.get(
    "/{reserva_id}",
    response_model=ReservaOut,
    responses=RESPUESTAS,
    summary="Detalle y seguimiento de una reserva",
)
def detalle(
    reserva_id: int,
    usuario: Usuario = Depends(requiere_algun_permiso(*LECTURA)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ReservaOut:
    reserva = reserva_service.obtener(db, reserva_id, usuario, permisos)
    return armar_reserva(db, reserva, permisos)


@router.post(
    "/{reserva_id}/reprogramacion",
    response_model=ReservaOut,
    responses=RESPUESTAS,
    summary="Reprogramar una reserva a otro bloque",
)
def reprogramar(
    reserva_id: int,
    datos: ReprogramacionIn,
    usuario: Usuario = Depends(requiere_permiso("reserva:reprogramar")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ReservaOut:
    """RF-015. Libera el bloque anterior, toma el nuevo y avisa al cliente.

    `RN-06`: como máximo **dos** reprogramaciones; la tercera responde `422
    LIMITE_DE_REPROGRAMACIONES` ofreciendo cancelar y reservar de nuevo
    (`CA-01`). `2b`: deben faltar **más de dos horas** para el inicio, si no
    responde `422 REPROGRAMACION_FUERA_DE_PLAZO`.

    El bloque nuevo pasa por las mismas reglas que uno recién creado: `RN-02`
    (60 minutos de anticipación), `RN-07` con feriados y bloqueos de la agenda,
    y `RN-03` (una bahía atiende un vehículo a la vez). La tarifa **no** se
    recalcula: `RF-014 CA-03` la congeló al crear la reserva.
    """
    reserva = reserva_service.obtener(db, reserva_id, usuario, permisos)
    reserva = reserva_service.reprogramar(db, reserva, datos, usuario, permisos)
    return armar_reserva(db, reserva, permisos)


@router.post(
    "/{reserva_id}/cancelacion",
    response_model=ReservaOut,
    responses=RESPUESTAS,
    summary="Cancelar una reserva",
)
def cancelar(
    reserva_id: int,
    datos: CancelacionIn,
    usuario: Usuario = Depends(requiere_permiso("reserva:cancelar")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ReservaOut:
    reserva = reserva_service.obtener(db, reserva_id, usuario, permisos)
    reserva, penalidad = reserva_service.cancelar(db, reserva, datos.motivo, usuario, permisos)
    return armar_reserva(db, reserva, permisos, penalidad=penalidad)
