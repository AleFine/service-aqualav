"""Loyalty programme endpoints (RF-032, RN-11).

Three reads and one write, all of them about the CALLER and nobody else: the
balance, the statement and the coupons come from ``usuario.id`` and the
redemption spends ``usuario.id``'s points. There is no "points of user X"
endpoint, so the horizontal authorization of RF-017 CA-03 has nothing to get
wrong here - the identity is the token.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import (
    BeneficioOut,
    CanjeIn,
    CuponCanjeOut,
    ErrorBody,
    Lista,
    SaldoPuntosOut,
)
from app.services import fidelizacion_service
from app.services.ensamblador import armar_beneficio, armar_cupon_canje, armar_saldo_puntos

router = APIRouter(prefix="/fidelizacion", tags=["fidelización"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.get(
    "/saldo",
    response_model=SaldoPuntosOut,
    responses=RESPUESTAS,
    summary="Saldo de puntos, movimientos y cupones",
)
def saldo(
    usuario: Usuario = Depends(requiere_permiso(fidelizacion_service.PERMISO_LEER)),
    db: Session = Depends(get_db),
) -> SaldoPuntosOut:
    """RF-032: «saldo y movimientos de puntos», más los cupones ya canjeados.

    El saldo es la **suma de los movimientos**, nunca un contador guardado: no
    hay una segunda copia que pueda discrepar del historial que la explica.
    """
    return armar_saldo_puntos(fidelizacion_service.saldo(db, usuario))


@router.get(
    "/beneficios",
    response_model=Lista[BeneficioOut],
    responses=RESPUESTAS,
    summary="Beneficios canjeables",
)
def listar_beneficios(
    usuario: Usuario = Depends(requiere_permiso(fidelizacion_service.PERMISO_LEER)),
    db: Session = Depends(get_db),
) -> Lista[BeneficioOut]:
    """RF-032 `4b`: un beneficio **agotado o vencido se retira del listado**.

    No lo barre nada: la consulta pide stock y vigencia, igual que una promoción
    vencida deja de aplicarse sola (RF-011 `4a`). Cada fila dice además si al
    cliente le alcanza y, si no, cuántos puntos le faltan.
    """
    puntos = fidelizacion_service.saldo(db, usuario).puntos
    return Lista[BeneficioOut](
        items=[
            armar_beneficio(beneficio, puntos=puntos)
            for beneficio in fidelizacion_service.beneficios(db)
        ]
    )


@router.post(
    "/canjes",
    response_model=CuponCanjeOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Canjear puntos por un beneficio",
)
def canjear(
    datos: CanjeIn,
    usuario: Usuario = Depends(requiere_permiso(fidelizacion_service.PERMISO_CANJEAR)),
    db: Session = Depends(get_db),
) -> CuponCanjeOut:
    """RF-032 `CA-02`: con saldo insuficiente responde **422**, diciendo cuántos
    puntos faltan (`detalles.puntos_faltantes`).

    Con saldo suficiente descuenta los puntos y devuelve un **cupón**. Ese
    código entra en `POST /reservas` por el campo `cupon` de siempre: `RF-012`
    sigue siendo el único sitio donde se calcula una tarifa (`RN-04`).
    """
    return armar_cupon_canje(fidelizacion_service.canjear(db, usuario, datos.beneficio_id))
