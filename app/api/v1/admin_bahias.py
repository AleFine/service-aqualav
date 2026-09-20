"""Bay administration (closes a v1.0 gap the MVP left open).

The MVP seeded four bays and had no way to add, rename or retire one. RF-018
and RF-020 both need them to be editable data, so they get their own permission
code, ``bahia:administrar``.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import BahiaActualizar, BahiaCrear, BahiaOut, ErrorBody, Lista
from app.services import bahia_service
from app.services.ensamblador import armar_bahia

router = APIRouter(prefix="/admin/bahias", tags=["administración"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.get(
    "",
    response_model=Lista[BahiaOut],
    responses=RESPUESTAS,
    summary="Listar las bahías, activas e inactivas",
)
def listar(
    _: Usuario = Depends(requiere_permiso(bahia_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> Lista[BahiaOut]:
    return Lista[BahiaOut](items=[armar_bahia(bahia) for bahia in bahia_service.listar(db)])


@router.post(
    "",
    response_model=BahiaOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Registrar una bahía",
)
def crear(
    datos: BahiaCrear,
    autor: Usuario = Depends(requiere_permiso(bahia_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> BahiaOut:
    return armar_bahia(bahia_service.crear(db, datos, autor))


@router.patch(
    "/{bahia_id}",
    response_model=BahiaOut,
    responses=RESPUESTAS,
    summary="Renombrar, activar, desactivar o liberar una bahía",
)
def actualizar(
    bahia_id: int,
    datos: BahiaActualizar,
    autor: Usuario = Depends(requiere_permiso(bahia_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> BahiaOut:
    return armar_bahia(bahia_service.actualizar(db, bahia_id, datos, autor))
