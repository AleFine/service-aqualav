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


@router.get(
    "/{servicio_id}",
    response_model=ServicioOut,
    responses=RESPUESTAS,
    summary="Detalle de un servicio, activo o inactivo",
)
def detalle(
    servicio_id: int,
    _: Usuario = Depends(requiere_permiso("servicio:administrar")),
    db: Session = Depends(get_db),
) -> ServicioOut:
    """The administrator's direct door to an inactive service.

    ``GET /servicios/{id}`` answers 404 for one that is disabled (RF-010
    CA-03), which forced the admin client to list the whole catalogue and
    filter it client side just to reopen the service it had disabled a moment
    earlier. This is the same detail, without the detour.
    """
    return armar_servicio(servicio_service.obtener(db, servicio_id))


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
