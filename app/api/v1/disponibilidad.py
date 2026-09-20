"""Availability of time blocks (RF-013)."""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import DisponibilidadOut, ErrorBody
from app.services import disponibilidad_service

router = APIRouter(prefix="/disponibilidad", tags=["disponibilidad"])


@router.get(
    "",
    response_model=DisponibilidadOut,
    responses={401: {"model": ErrorBody}, 403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
    summary="Bloques libres de una fecha para un servicio",
)
def consultar(
    fecha: date = Query(description="Fecha a consultar, en formato YYYY-MM-DD (hora de Lima)."),
    servicio_id: int = Query(description="Servicio cuya duración define el tamaño del bloque."),
    _: Usuario = Depends(requiere_permiso("disponibilidad:leer")),
    db: Session = Depends(get_db),
) -> DisponibilidadOut:
    return disponibilidad_service.consultar(db, fecha, servicio_id)
