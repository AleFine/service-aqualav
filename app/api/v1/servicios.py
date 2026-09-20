"""Public service catalog (RF-009).

v1.0 adds the vehicle dimension: the catalogue can be asked for the price that
applies to a given vehicle - by id, which must be the caller's own, or by type
for counter staff quoting a walk-in. The REGULAR price keeps travelling in
``precio`` so the app can show one struck through and the other highlighted.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.models.enums import TipoVehiculo
from app.schemas import ErrorBody, Lista, ServicioOut
from app.services import servicio_service, tarifa_service
from app.services.ensamblador import armar_servicio, armar_servicios

router = APIRouter(prefix="/servicios", tags=["servicios"])

RESPUESTAS = {401: {"model": ErrorBody}, 403: {"model": ErrorBody}, 404: {"model": ErrorBody}}

DESCRIPCION_VEHICULO = "Vehículo del cliente cuyo precio aplicable se quiere ver (RF-009 CA-02)."
DESCRIPCION_TIPO = "Tipo de vehículo, cuando no se quiere nombrar uno concreto."


@router.get(
    "",
    response_model=Lista[ServicioOut],
    responses=RESPUESTAS,
    summary="Catálogo de servicios activos",
)
def listar(
    vehiculo_id: int | None = Query(default=None, ge=1, description=DESCRIPCION_VEHICULO),
    tipo_vehiculo: TipoVehiculo | None = Query(default=None, description=DESCRIPCION_TIPO),
    usuario: Usuario = Depends(requiere_permiso("servicio:leer")),
    db: Session = Depends(get_db),
) -> Lista[ServicioOut]:
    tipo = tarifa_service.tipo_de_vehiculo(
        db,
        usuario,
        vehiculo_id=vehiculo_id,
        tipo_vehiculo=tipo_vehiculo.value if tipo_vehiculo else None,
    )
    servicios, aplicables = servicio_service.listar_publico(db, tipo_vehiculo=tipo)
    return Lista[ServicioOut](items=armar_servicios(servicios, aplicables))


@router.get(
    "/{servicio_id}",
    response_model=ServicioOut,
    responses=RESPUESTAS,
    summary="Detalle de un servicio",
)
def detalle(
    servicio_id: int,
    vehiculo_id: int | None = Query(default=None, ge=1, description=DESCRIPCION_VEHICULO),
    tipo_vehiculo: TipoVehiculo | None = Query(default=None, description=DESCRIPCION_TIPO),
    usuario: Usuario = Depends(requiere_permiso("servicio:leer")),
    db: Session = Depends(get_db),
) -> ServicioOut:
    tipo = tarifa_service.tipo_de_vehiculo(
        db,
        usuario,
        vehiculo_id=vehiculo_id,
        tipo_vehiculo=tipo_vehiculo.value if tipo_vehiculo else None,
    )
    servicio, aplicable = servicio_service.detalle_publico(db, servicio_id, tipo_vehiculo=tipo)
    return armar_servicio(servicio, aplicable)
