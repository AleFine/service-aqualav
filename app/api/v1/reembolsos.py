"""Annulments and refunds (RF-028).

The reversal hangs off the PAYMENT and not off the reservation, because that
is what is being reversed: a booking may have been charged more than once and
each charge has its own balance.

``Idempotency-Key`` is mandatory here too (`RNF-017` M1), so replaying a key
answers 200 with the refund that already exists instead of giving the money
back twice.
"""

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import ErrorBody, Lista, ReembolsoCrear, ReembolsoOut
from app.services import reembolso_service
from app.services.ensamblador import armar_reembolso

router = APIRouter(prefix="/pagos", tags=["pagos"])

PERMISO_REEMBOLSAR = "pago:reembolsar"

RESPUESTAS = {
    400: {"model": ErrorBody},
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.post(
    "/{pago_id}/reembolsos",
    response_model=ReembolsoOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {
            "model": ReembolsoOut,
            "description": "Reintento con la misma clave de idempotencia.",
        },
        **RESPUESTAS,
    },
    summary="Anular o reembolsar un pago",
)
def solicitar(
    pago_id: int,
    datos: ReembolsoCrear,
    respuesta: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    autor: Usuario = Depends(requiere_permiso(PERMISO_REEMBOLSAR)),
    db: Session = Depends(get_db),
) -> ReembolsoOut:
    """RF-028.

    `CA-01` el saldo baja en ese monto; `CA-02` un monto mayor al saldo
    responde **422**; `3a` una reversión que la pasarela rechaza queda
    `pendiente_manual` —201 igualmente, porque la solicitud sí se registró—.
    """
    clave = reembolso_service.resolver_clave(idempotency_key, datos.idempotency_key)
    pago = reembolso_service.obtener_pago(db, pago_id)
    reembolso, creado = reembolso_service.solicitar(
        db,
        pago,
        tipo=datos.tipo.value,
        motivo=datos.motivo,
        idempotency_key=clave,
        monto_centimos=datos.monto_centimos,
        autor=autor,
    )
    if not creado:
        respuesta.status_code = status.HTTP_200_OK
    return armar_reembolso(reembolso)


@router.get(
    "/{pago_id}/reembolsos",
    response_model=Lista[ReembolsoOut],
    responses=RESPUESTAS,
    summary="Reembolsos registrados sobre un pago",
)
def listar(
    pago_id: int,
    _: Usuario = Depends(requiere_permiso(PERMISO_REEMBOLSAR)),
    db: Session = Depends(get_db),
) -> Lista[ReembolsoOut]:
    """`3a`: aquí es donde se ven las reversiones pendientes de gestión manual."""
    pago = reembolso_service.obtener_pago(db, pago_id)
    return Lista[ReembolsoOut](
        items=[armar_reembolso(fila) for fila in reembolso_service.listar(db, pago)]
    )
