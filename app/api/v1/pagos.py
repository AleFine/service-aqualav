"""Payment registration (RF-026)."""

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.orm import Session

from app.deps import get_db, permisos_actuales, requiere_permiso
from app.models import Usuario
from app.schemas import ErrorBody, PagoCrear, PagoOut
from app.services import pago_service, reserva_service
from app.services.ensamblador import armar_pago

router = APIRouter(prefix="/reservas", tags=["pagos"])


@router.post(
    "/{reserva_id}/pagos",
    response_model=PagoOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": PagoOut, "description": "Reintento con la misma clave de idempotencia."},
        400: {"model": ErrorBody},
        401: {"model": ErrorBody},
        403: {"model": ErrorBody},
        404: {"model": ErrorBody},
        422: {"model": ErrorBody},
    },
    summary="Registrar el cobro presencial de una reserva",
)
def registrar(
    reserva_id: int,
    datos: PagoCrear,
    respuesta: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    autor: Usuario = Depends(requiere_permiso("pago:registrar")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> PagoOut:
    """201 on the first call, 200 when the idempotency key is replayed (CA-02)."""
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    pago, creado = pago_service.registrar(db, reserva, datos, autor, idempotency_key)
    if not creado:
        respuesta.status_code = status.HTTP_200_OK
    return armar_pago(pago)
