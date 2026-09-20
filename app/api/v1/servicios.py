"""Public service catalog (RF-009)."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import ErrorBody, Lista, ServicioOut
from app.services import servicio_service
from app.services.ensamblador import armar_servicio

router = APIRouter(prefix="/servicios", tags=["servicios"])

RESPUESTAS = {401: {"model": ErrorBody}, 403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


@router.get(
    "",
    response_model=Lista[ServicioOut],
    responses=RESPUESTAS,
    summary="Catálogo de servicios activos",
)
def listar(
    _: Usuario = Depends(requiere_permiso("servicio:leer")),
    db: Session = Depends(get_db),
) -> Lista[ServicioOut]:
    servicios = servicio_service.listar_publico(db)
    return Lista[ServicioOut](items=[armar_servicio(s) for s in servicios])


@router.get(
    "/{servicio_id}",
    response_model=ServicioOut,
    responses=RESPUESTAS,
    summary="Detalle de un servicio",
)
def detalle(
    servicio_id: int,
    _: Usuario = Depends(requiere_permiso("servicio:leer")),
    db: Session = Depends(get_db),
) -> ServicioOut:
    return armar_servicio(servicio_service.obtener_publico(db, servicio_id))
