"""Tariff administration: factors, packages, promotions and add-ons.

RF-010 (delta) and RF-011. The factors sit behind ``servicio:administrar``
because they ARE the service's tariff - the same screen that sets the base
price sets them - while packages, promotions and add-ons sit behind
``promocion:administrar``, which RF-011 grants to the administrator alone.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import (
    AdicionalActualizar,
    AdicionalIn,
    AdicionalOut,
    ErrorBody,
    FactorIn,
    FactorOut,
    Lista,
    PaqueteActualizar,
    PaqueteIn,
    PaqueteOut,
    PromocionActualizar,
    PromocionIn,
    PromocionOut,
)
from app.services import promocion_service, tarifa_service
from app.services.ensamblador import (
    armar_adicional,
    armar_factor,
    armar_paquete,
    armar_promocion,
)

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}

factores_router = APIRouter(prefix="/admin/factores", tags=["administración"])
paquetes_router = APIRouter(prefix="/admin/paquetes", tags=["administración"])
promociones_router = APIRouter(prefix="/admin/promociones", tags=["administración"])
adicionales_router = APIRouter(prefix="/admin/adicionales", tags=["administración"])


# --------------------------------------------------------------------------
# Vehicle factors (RF-010 delta, RN-04)
# --------------------------------------------------------------------------
@factores_router.get(
    "",
    response_model=Lista[FactorOut],
    responses=RESPUESTAS,
    summary="Factores por tipo de vehículo vigentes",
)
def listar_factores(
    _: Usuario = Depends(requiere_permiso(tarifa_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> Lista[FactorOut]:
    return Lista[FactorOut](items=[armar_factor(f) for f in tarifa_service.listar_factores(db)])


@factores_router.put(
    "",
    response_model=FactorOut,
    responses=RESPUESTAS,
    summary="Definir el factor de un tipo de vehículo",
)
def definir_factor(
    datos: FactorIn,
    autor: Usuario = Depends(requiere_permiso(tarifa_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> FactorOut:
    """Idempotent by ``(servicio_id, tipo_vehiculo)``, hence a PUT.

    The previous value is not overwritten: its row is closed and a new one is
    opened, and the change is audited with both values (RNF-014).
    """
    return armar_factor(tarifa_service.definir_factor(db, datos, autor))


# --------------------------------------------------------------------------
# Packages (RF-011)
# --------------------------------------------------------------------------
@paquetes_router.get(
    "",
    response_model=Lista[PaqueteOut],
    responses=RESPUESTAS,
    summary="Paquetes, activos e inactivos",
)
def listar_paquetes(
    _: Usuario = Depends(requiere_permiso(promocion_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> Lista[PaqueteOut]:
    paquetes = promocion_service.listar_paquetes(db, solo_activos=False)
    return Lista[PaqueteOut](items=[armar_paquete(paquete) for paquete in paquetes])


@paquetes_router.post(
    "",
    response_model=PaqueteOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Publicar un paquete",
)
def crear_paquete(
    datos: PaqueteIn,
    autor: Usuario = Depends(requiere_permiso(promocion_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> PaqueteOut:
    return armar_paquete(promocion_service.crear_paquete(db, datos, autor))


@paquetes_router.patch(
    "/{paquete_id}",
    response_model=PaqueteOut,
    responses=RESPUESTAS,
    summary="Editar, activar o desactivar un paquete",
)
def actualizar_paquete(
    paquete_id: int,
    datos: PaqueteActualizar,
    autor: Usuario = Depends(requiere_permiso(promocion_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> PaqueteOut:
    return armar_paquete(promocion_service.actualizar_paquete(db, paquete_id, datos, autor))


# --------------------------------------------------------------------------
# Promotions (RF-011)
# --------------------------------------------------------------------------
@promociones_router.get(
    "",
    response_model=Lista[PromocionOut],
    responses=RESPUESTAS,
    summary="Promociones, vigentes y vencidas",
)
def listar_promociones(
    _: Usuario = Depends(requiere_permiso(promocion_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> Lista[PromocionOut]:
    """The administrator sees the expired ones too: they are the history."""
    todas = promocion_service.listar_promociones(db, solo_activas=False)
    vigentes = {promo.id for promo in promocion_service.listar_vigentes(db)}
    return Lista[PromocionOut](
        items=[armar_promocion(promo, vigente=promo.id in vigentes) for promo in todas]
    )


@promociones_router.post(
    "",
    response_model=PromocionOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Publicar una promoción",
)
def crear_promocion(
    datos: PromocionIn,
    autor: Usuario = Depends(requiere_permiso(promocion_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> PromocionOut:
    """409 ``PROMOCION_SOLAPADA`` when the range collides (RF-011 flow 2a)."""
    return armar_promocion(promocion_service.crear_promocion(db, datos, autor))


@promociones_router.patch(
    "/{promocion_id}",
    response_model=PromocionOut,
    responses=RESPUESTAS,
    summary="Ajustar el rango, el valor o la vigencia de una promoción",
)
def actualizar_promocion(
    promocion_id: int,
    datos: PromocionActualizar,
    autor: Usuario = Depends(requiere_permiso(promocion_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> PromocionOut:
    return armar_promocion(promocion_service.actualizar_promocion(db, promocion_id, datos, autor))


# --------------------------------------------------------------------------
# Add-ons
# --------------------------------------------------------------------------
@adicionales_router.get(
    "",
    response_model=Lista[AdicionalOut],
    responses=RESPUESTAS,
    summary="Adicionales, activos e inactivos",
)
def listar_adicionales(
    _: Usuario = Depends(requiere_permiso(promocion_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> Lista[AdicionalOut]:
    adicionales = promocion_service.listar_adicionales(db, solo_activos=False)
    return Lista[AdicionalOut](items=[armar_adicional(item) for item in adicionales])


@adicionales_router.post(
    "",
    response_model=AdicionalOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Registrar un adicional",
)
def crear_adicional(
    datos: AdicionalIn,
    autor: Usuario = Depends(requiere_permiso(promocion_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> AdicionalOut:
    return armar_adicional(promocion_service.crear_adicional(db, datos, autor))


@adicionales_router.patch(
    "/{adicional_id}",
    response_model=AdicionalOut,
    responses=RESPUESTAS,
    summary="Editar, activar o desactivar un adicional",
)
def actualizar_adicional(
    adicional_id: int,
    datos: AdicionalActualizar,
    autor: Usuario = Depends(requiere_permiso(promocion_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> AdicionalOut:
    return armar_adicional(promocion_service.actualizar_adicional(db, adicional_id, datos, autor))


router = APIRouter()
router.include_router(factores_router)
router.include_router(paquetes_router)
router.include_router(promociones_router)
router.include_router(adicionales_router)

__all__ = ["router"]
