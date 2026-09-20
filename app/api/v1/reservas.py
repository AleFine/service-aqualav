"""Reservation endpoints (RF-014, RF-016, RF-017, RF-022)."""

import math

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
    estado: str | None = Query(default=None, max_length=30, description="Filtro por estado."),
    pagina: int = Query(default=1, ge=1),
    tamanio: int = Query(default=TAMANIO_PAGINA_DEFECTO, ge=1, le=TAMANIO_PAGINA_MAXIMO),
    usuario: Usuario = Depends(requiere_algun_permiso(*LECTURA)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> Pagina[ReservaOut]:
    items, total, pagina, tamanio = reserva_service.listar(
        db,
        usuario,
        permisos,
        estado=estado or None,
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
