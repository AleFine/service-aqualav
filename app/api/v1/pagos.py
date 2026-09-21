"""Payments and receipts (RF-025, RF-026, RF-027).

Four doors, one per requirement:

* ``POST /reservas/{id}/pagos`` - the counter charge of the MVP;
* ``POST /reservas/{id}/pagos/en-linea`` - the gateway charge (RF-026 v1.0);
* ``POST /reservas/{id}/modalidad-pago`` - choosing and changing the modality
  (RF-025);
* ``GET /reservas/{id}/comprobante`` and ``GET /comprobantes/{id}/archivo`` -
  the receipt and its PDF (RF-027).

Routers stay a parse/delegate/map sandwich: every rule, every state move and
every call to the simulated gateway lives in ``pago_service``.
"""

from fastapi import APIRouter, Depends, Header, Response, status
from fastapi.responses import Response as RespuestaBinaria
from sqlalchemy.orm import Session

from app.deps import get_db, permisos_actuales, requiere_algun_permiso, requiere_permiso
from app.models import Usuario
from app.schemas import (
    ComprobanteOut,
    ErrorBody,
    ModalidadPagoIn,
    PagoCrear,
    PagoEnLineaCrear,
    PagoOut,
    ReservaOut,
)
from app.services import comprobante_service, pago_service, reserva_service
from app.services.ensamblador import armar_comprobante, armar_pago, armar_reserva
from app.services.proveedores.documentos import MIME_PDF

router = APIRouter(prefix="/reservas", tags=["pagos"])

#: Both reading permissions reach a receipt; the horizontal filter inside
#: ``reserva_service.obtener`` is what keeps a customer to their own (RF-017
#: CA-03). A receipt is part of the reservation, so it needs no permission of
#: its own - it would only be a second place to get the same rule wrong.
LECTURA = ("reserva:leer_propias", "reserva:leer_todas")

RESPUESTAS = {
    400: {"model": ErrorBody},
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.post(
    "/{reserva_id}/pagos",
    response_model=PagoOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": PagoOut, "description": "Reintento con la misma clave de idempotencia."},
        **RESPUESTAS,
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


@router.post(
    "/{reserva_id}/pagos/en-linea",
    response_model=ReservaOut,
    responses={
        **RESPUESTAS,
        503: {
            "model": ErrorBody,
            "description": "Pasarela caída: se ofrece continuar con pago presencial (RF-025 3a).",
        },
    },
    summary="Cobrar una reserva a través de la pasarela",
)
def cobrar_en_linea(
    reserva_id: int,
    datos: PagoEnLineaCrear,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    autor: Usuario = Depends(requiere_permiso("pago:en_linea")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ReservaOut:
    """RF-026 v1.0.

    Devuelve la reserva porque un cobro aprobado la confirma: el cliente
    necesita el estado nuevo, no solo el pago. Un rechazo responde `422
    PAGO_RECHAZADO` con el motivo, y la pasarela caída `503
    PASARELA_NO_DISPONIBLE` ofreciendo la alternativa presencial.
    """
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    pago_service.cobrar_en_linea(db, reserva, datos, autor, permisos, idempotency_key)
    return armar_reserva(db, reserva, permisos)


@router.post(
    "/{reserva_id}/modalidad-pago",
    response_model=ReservaOut,
    responses=RESPUESTAS,
    summary="Elegir o cambiar la modalidad de pago de una reserva",
)
def cambiar_modalidad(
    reserva_id: int,
    datos: ModalidadPagoIn,
    autor: Usuario = Depends(requiere_permiso("pago:en_linea")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ReservaOut:
    """RF-025 `4a`: se puede cambiar mientras el pago no esté confirmado."""
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    reserva = pago_service.cambiar_modalidad(db, reserva, datos.modalidad.value, autor, permisos)
    return armar_reserva(db, reserva, permisos)


@router.get(
    "/{reserva_id}/comprobante",
    response_model=ComprobanteOut,
    responses=RESPUESTAS,
    summary="Comprobante electrónico de una reserva",
)
def comprobante_de_reserva(
    reserva_id: int,
    autor: Usuario = Depends(requiere_algun_permiso(*LECTURA)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ComprobanteOut:
    """RF-027 `CA-02`: el historial enlaza el comprobante."""
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    return armar_comprobante(comprobante_service.de_reserva(db, reserva))


#: Second router: the PDF hangs off the receipt, not off the reservation, so
#: its URL stays stable if a receipt is ever reissued against another booking.
comprobantes = APIRouter(prefix="/comprobantes", tags=["pagos"])


@comprobantes.get(
    "/{comprobante_id}/archivo",
    responses={
        200: {
            "content": {MIME_PDF: {}},
            "description": "El PDF del comprobante.",
        },
        **RESPUESTAS,
    },
    summary="Descargar el PDF del comprobante",
)
def descargar_comprobante(
    comprobante_id: int,
    autor: Usuario = Depends(requiere_algun_permiso(*LECTURA)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> RespuestaBinaria:
    """RF-027 `CA-02`: «puede descargarse en PDF».

    La autorización horizontal pasa por la reserva: pedir el comprobante de
    otro cliente responde 404 igual que pedir su reserva.
    """
    comprobante = comprobante_service.obtener(db, comprobante_id)
    reserva_service.obtener(db, comprobante.reserva_id, autor, permisos)
    contenido = comprobante_service.archivo(comprobante)
    return RespuestaBinaria(
        content=contenido,
        media_type=MIME_PDF,
        headers={
            "Content-Disposition": f'attachment; filename="{comprobante.numero}.pdf"',
        },
    )
