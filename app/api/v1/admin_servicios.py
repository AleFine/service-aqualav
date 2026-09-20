"""Service and tariff administration (RF-010)."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import ErrorBody, Lista, ServicioActualizar, ServicioCrear, ServicioOut
from app.services import servicio_service
from app.services.ensamblador import armar_servicio

router = APIRouter(prefix="/admin/servicios", tags=["administración"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.get(
    "",
    response_model=Lista[ServicioOut],
    responses=RESPUESTAS,
    summary="Catálogo completo, activos e inactivos",
)
def listar(
    _: Usuario = Depends(requiere_permiso("servicio:administrar")),
    db: Session = Depends(get_db),
) -> Lista[ServicioOut]:
    servicios = servicio_service.listar_administracion(db)
    return Lista[ServicioOut](items=[armar_servicio(s) for s in servicios])


@router.post(
    "",
    response_model=ServicioOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Crear un servicio con su primer precio",
)
def crear(
    datos: ServicioCrear,
    autor: Usuario = Depends(requiere_permiso("servicio:administrar")),
    db: Session = Depends(get_db),
) -> ServicioOut:
    return armar_servicio(servicio_service.crear(db, datos, autor))


@router.patch(
    "/{servicio_id}",
    response_model=ServicioOut,
    responses=RESPUESTAS,
    summary="Editar un servicio o abrir una nueva vigencia de precio",
)
def actualizar(
    servicio_id: int,
    datos: ServicioActualizar,
    autor: Usuario = Depends(requiere_permiso("servicio:administrar")),
    db: Session = Depends(get_db),
) -> ServicioOut:
    return armar_servicio(servicio_service.actualizar(db, servicio_id, datos, autor))
