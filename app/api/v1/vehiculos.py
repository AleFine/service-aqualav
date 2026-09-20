"""Vehicle endpoints (RF-007)."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import ErrorBody, Lista, VehiculoIn, VehiculoOut
from app.services import vehiculo_service

router = APIRouter(prefix="/vehiculos", tags=["vehículos"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
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
    usuario: Usuario = Depends(requiere_permiso("vehiculo:leer")),
    db: Session = Depends(get_db),
) -> Lista[VehiculoOut]:
    vehiculos = vehiculo_service.listar(db, usuario)
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
