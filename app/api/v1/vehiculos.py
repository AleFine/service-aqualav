"""Vehicle endpoints (RF-007, RF-008, RN-01)."""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import ErrorBody, Lista, VehiculoActualizar, VehiculoIn, VehiculoOut
from app.services import vehiculo_service

router = APIRouter(prefix="/vehiculos", tags=["vehículos"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.get(
    "",
    response_model=Lista[VehiculoOut],
    responses=RESPUESTAS,
    summary="Listar los vehículos propios",
)
def listar(
    incluir_inactivos: bool = Query(
        default=False,
        description="Incluye los vehículos dados de baja (RF-008 CA-02).",
    ),
    usuario: Usuario = Depends(requiere_permiso("vehiculo:leer")),
    db: Session = Depends(get_db),
) -> Lista[VehiculoOut]:
    vehiculos = vehiculo_service.listar(db, usuario, incluir_inactivos=incluir_inactivos)
    return Lista[VehiculoOut](items=[VehiculoOut.model_validate(v) for v in vehiculos])


@router.post(
    "",
    response_model=VehiculoOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Registrar un vehículo",
)
def crear(
    datos: VehiculoIn,
    usuario: Usuario = Depends(requiere_permiso("vehiculo:crear")),
    db: Session = Depends(get_db),
) -> VehiculoOut:
    return VehiculoOut.model_validate(vehiculo_service.crear(db, usuario, datos))


@router.patch(
    "/{vehiculo_id}",
    response_model=VehiculoOut,
    responses=RESPUESTAS,
    summary="Editar un vehículo propio",
)
def actualizar(
    vehiculo_id: int,
    datos: VehiculoActualizar,
    usuario: Usuario = Depends(requiere_permiso("vehiculo:editar")),
    db: Session = Depends(get_db),
) -> VehiculoOut:
    return VehiculoOut.model_validate(vehiculo_service.actualizar(db, usuario, vehiculo_id, datos))


@router.delete(
    "/{vehiculo_id}",
    response_model=VehiculoOut,
    responses=RESPUESTAS,
    summary="Dar de baja un vehículo propio",
)
def dar_de_baja(
    vehiculo_id: int,
    usuario: Usuario = Depends(requiere_permiso("vehiculo:eliminar")),
    db: Session = Depends(get_db),
) -> VehiculoOut:
    """RF-008: the deletion is LOGICAL, so it answers with the vehicle itself.

    A 204 would say "it is gone"; it is not. It left the active list and kept
    its whole history (CA-02), and the body is what shows that.
    """
    return VehiculoOut.model_validate(vehiculo_service.dar_de_baja(db, usuario, vehiculo_id))


@router.post(
    "/{vehiculo_id}/verificacion",
    response_model=VehiculoOut,
    responses=RESPUESTAS,
    summary="Verificar un vehículo (RN-01)",
)
def verificar(
    vehiculo_id: int,
    autor: Usuario = Depends(requiere_permiso(vehiculo_service.PERMISO_VERIFICAR)),
    db: Session = Depends(get_db),
) -> VehiculoOut:
    """RN-01 v1.0: the counter confirms the plate on the card is the one on the car."""
    return VehiculoOut.model_validate(vehiculo_service.verificar(db, autor, vehiculo_id))
