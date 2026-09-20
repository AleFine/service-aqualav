"""Customer-facing tariff catalogue: packages, promotions, add-ons and quotes.

RF-011 and RF-012. Four read paths that share one audience and one permission
(``servicio:leer``), so they live in one module with one router per resource,
mounted together at the bottom.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import (
    AdicionalOut,
    DesgloseOut,
    ErrorBody,
    Lista,
    PaqueteOut,
    PromocionOut,
    TarifaCalculoIn,
)
from app.services import promocion_service, servicio_service, tarifa_service
from app.services.ensamblador import (
    armar_adicional,
    armar_desglose,
    armar_paquete,
    armar_promocion,
)

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    422: {"model": ErrorBody},
}

paquetes_router = APIRouter(prefix="/paquetes", tags=["catálogo"])
promociones_router = APIRouter(prefix="/promociones", tags=["catálogo"])
adicionales_router = APIRouter(prefix="/adicionales", tags=["catálogo"])
tarifas_router = APIRouter(prefix="/tarifas", tags=["catálogo"])


@paquetes_router.get(
    "",
    response_model=Lista[PaqueteOut],
    responses=RESPUESTAS,
    summary="Paquetes activos y su precio preferencial",
)
def listar_paquetes(
    _: Usuario = Depends(requiere_permiso(promocion_service.PERMISO_LEER)),
    db: Session = Depends(get_db),
) -> Lista[PaqueteOut]:
    items = []
    for paquete in promocion_service.listar_paquetes(db, solo_activos=True):
        promocional, promocion = tarifa_service.precio_de_paquete(db, paquete)
        items.append(armar_paquete(paquete, promocional_centimos=promocional, promocion=promocion))
    return Lista[PaqueteOut](items=items)


@promociones_router.get(
    "",
    response_model=Lista[PromocionOut],
    responses=RESPUESTAS,
    summary="Promociones vigentes hoy",
)
def listar_promociones(
    _: Usuario = Depends(requiere_permiso(promocion_service.PERMISO_LEER)),
    db: Session = Depends(get_db),
) -> Lista[PromocionOut]:
    """An expired promotion is simply not here any more (RF-011 flow 4a)."""
    vigentes = promocion_service.listar_vigentes(db)
    return Lista[PromocionOut](items=[armar_promocion(promo) for promo in vigentes])


@adicionales_router.get(
    "",
    response_model=Lista[AdicionalOut],
    responses=RESPUESTAS,
    summary="Adicionales disponibles",
)
def listar_adicionales(
    _: Usuario = Depends(requiere_permiso(promocion_service.PERMISO_LEER)),
    db: Session = Depends(get_db),
) -> Lista[AdicionalOut]:
    adicionales = promocion_service.listar_adicionales(db, solo_activos=True)
    return Lista[AdicionalOut](items=[armar_adicional(item) for item in adicionales])


@tarifas_router.post(
    "/calculo",
    response_model=DesgloseOut,
    responses=RESPUESTAS,
    summary="Calcular la tarifa de un servicio antes de reservar",
)
def calcular(
    datos: TarifaCalculoIn,
    usuario: Usuario = Depends(requiere_permiso(promocion_service.PERMISO_LEER)),
    db: Session = Depends(get_db),
) -> DesgloseOut:
    """RF-012: the same arithmetic the reservation freezes, without booking.

    A POST and not a GET because the two alternate flows LEAVE A TRACE: a
    rejected coupon (3a) and a total clamped to zero (4a) are written to the
    event log. The answer is the breakdown in every case, never an error.
    """
    return armar_desglose(servicio_service.cotizar(db, usuario, datos))


router = APIRouter()
router.include_router(paquetes_router)
router.include_router(promociones_router)
router.include_router(adicionales_router)
router.include_router(tarifas_router)

__all__ = ["router"]
