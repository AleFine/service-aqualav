"""Electronic receipts (RF-027).

One entry point, :func:`emitir`, called by every operation that leaves a
payment confirmed - the counter charge and the online one. It does four things
and the order matters:

1. **numbers** the receipt in a correlative series (CA-01). The number is taken
   inside the transaction that inserts the row and the pair
   ``(serie, numero_correlativo)`` is unique, so two receipts can never share
   one and the loser of a race simply takes the next;
2. **renders** it as a real PDF through ``ProveedorDocumentos`` - hand written,
   no new dependency (plan section 4);
3. **stores** the bytes through ``ProveedorAlmacenamiento`` under a key, never
   a path, so the file lives outside the database and can move without a
   migration;
4. **notifies** through ``notificacion_service.despachar`` with the
   ``comprobante`` event. That is INC-5's instruction taken literally: to send
   something new you add a template and an event, you do not write a dispatch
   by hand. It is also what makes flow 3a free - the dispatcher never raises,
   the failed delivery is a ``fallida`` row with its cause, and the receipt
   stays downloadable from the application.

Flow 1a ("fallo de generación -> reintento y aviso al administrador si
persiste") is honoured the same way a notification failure is: the receipt is
retried once per attempt and, if the document still cannot be produced, the
CHARGE is not rolled back - the money arrived - and the failure is written to
the event log for the administrator to pick up. Losing a payment because a PDF
could not be written would be the worse of the two outcomes by a wide margin.
"""

import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import ComprobanteNoDisponible, RecursoNoEncontrado, detalle
from app.core.horario import a_lima, ahora_utc, desde_bd
from app.models import Comprobante, EventoNotificacion, Pago, Reserva
from app.repositories import pago as pago_repo
from app.services import eventos, notificacion_service
from app.services.proveedores.almacenamiento import (
    ObjetoNoEncontrado,
    ProveedorAlmacenamiento,
    proveedor_almacenamiento,
)
from app.services.proveedores.documentos import (
    MIME_PDF,
    ProveedorDocumentos,
    proveedor_documentos,
)

logger = logging.getLogger("aqualav.comprobantes")

#: How many times a correlative collision is retried before giving up.
INTENTOS_CORRELATIVO = 5

#: Format the receipt prints every date in.
FORMATO_FECHA = "%d/%m/%Y %H:%M"


def clave_de_archivo(serie: str, numero: int) -> str:
    """Where the PDF lives in the object store. A KEY, never a path."""
    return f"comprobantes/{serie}/{serie}-{numero:08d}.pdf"


def _soles(centimos: int, moneda: str) -> str:
    """``2500`` -> ``PEN 25.00``. Integer cents in, text out (P6)."""
    return f"{moneda} {centimos // 100}.{abs(centimos) % 100:02d}"


def lineas_del_comprobante(
    reserva: Reserva, pago: Pago, numero: str, emitido_en: datetime
) -> list[str]:
    """The body of the document: the breakdown RF-027 CA-01 asks for.

    It is built from the FROZEN breakdown INC-2 stored on the reservation, not
    from today's catalogue, so a receipt reprinted next year still says what
    the customer actually paid.
    """
    lineas = [
        f"Comprobante: {numero}",
        f"Emitido: {a_lima(desde_bd(emitido_en)).strftime(FORMATO_FECHA)}",
        "",
        f"Cliente: {reserva.usuario.nombre_completo}",
        f"Documento: {reserva.usuario.numero_documento or '-'}",
        f"Reserva: {reserva.codigo}",
        f"Vehiculo: {reserva.vehiculo.placa} ({reserva.vehiculo.tipo})",
        f"Servicio: {reserva.servicio.nombre}",
        f"Fecha del servicio: {a_lima(desde_bd(reserva.inicio)).strftime(FORMATO_FECHA)}",
        "",
        "Detalle:",
    ]

    desglose = reserva.tarifa
    if desglose is not None:
        lineas.append(f"  Precio base: {_soles(desglose.precio_base_centimos, desglose.moneda)}")
        lineas.append(f"  Factor {desglose.tipo_vehiculo}: x{desglose.factor_milesimas / 1000:.3f}")
        lineas.append(
            f"  Base ajustada: {_soles(desglose.base_ajustada_centimos, desglose.moneda)}"
        )
        for adicional in reserva.adicionales:
            lineas.append(
                f"  Adicional {adicional.nombre}: "
                f"{_soles(adicional.monto_centimos, adicional.moneda)}"
            )
        if desglose.descuento_centimos:
            etiqueta = desglose.promocion_nombre or "Descuento"
            lineas.append(f"  {etiqueta}: -{_soles(desglose.descuento_centimos, desglose.moneda)}")
    else:  # pragma: no cover - only reservations created before INC-2
        lineas.append(f"  Servicio: {_soles(reserva.monto_centimos, reserva.moneda)}")

    lineas.extend(
        [
            "",
            f"TOTAL: {_soles(pago.monto_centimos, pago.moneda)}",
            f"Medio de pago: {pago.medio}",
            f"Modalidad: {reserva.modalidad_pago}",
        ]
    )
    if pago.referencia_externa:
        lineas.append(f"Referencia de la pasarela: {pago.referencia_externa}")
    lineas.append("")
    lineas.append("IGV incluido (RN-12). Gracias por elegir AquaLav.")
    return lineas


def _numerar_y_guardar(
    db: Session,
    reserva: Reserva,
    pago: Pago,
    *,
    emitido_en: datetime,
    documentos: ProveedorDocumentos,
    almacenamiento: ProveedorAlmacenamiento,
) -> Comprobante:
    """Take the next number of the series and write the row plus the file."""
    serie = settings.comprobante_serie
    ultimo_error: IntegrityError | None = None

    for _ in range(INTENTOS_CORRELATIVO):
        numero = pago_repo.siguiente_correlativo(db, serie)
        clave = clave_de_archivo(serie, numero)
        try:
            # A SAVEPOINT, not the whole transaction: a collision on the number
            # must never undo the payment that is being receipted.
            with db.begin_nested():
                fila = pago_repo.crear_comprobante(
                    db,
                    reserva_id=reserva.id,
                    pago_id=pago.id,
                    serie=serie,
                    numero_correlativo=numero,
                    monto_centimos=pago.monto_centimos,
                    moneda=pago.moneda,
                    medio_pago=pago.medio,
                    archivo_key=clave,
                    emitido_en=emitido_en,
                )
        except IntegrityError as error:  # pragma: no cover - needs a real race
            ultimo_error = error
            continue

        documento = documentos.comprobante(
            f"AquaLav - Comprobante {serie}-{numero:08d}",
            lineas_del_comprobante(reserva, pago, f"{serie}-{numero:08d}", emitido_en),
        )
        almacenamiento.guardar(clave, documento, MIME_PDF)
        return fila

    # pragma: no cover - five collisions in a row
    raise ultimo_error or IntegrityError("comprobante", None, Exception("correlativo"))


def emitir(
    db: Session,
    reserva: Reserva,
    pago: Pago,
    *,
    autor_id: int | None = None,
    momento: datetime | None = None,
    documentos: ProveedorDocumentos | None = None,
    almacenamiento: ProveedorAlmacenamiento | None = None,
    **proveedores,
) -> Comprobante | None:
    """Issue the receipt of a confirmed payment (RF-027).

    Returns the row, or ``None`` when the document could not be produced -
    which is flow 1a: the payment stands, the incident is in the event log and
    the administrator can reissue. It never raises for that reason, on purpose.
    """
    existente = pago_repo.obtener_comprobante_de_pago(db, pago.id)
    if existente is not None:
        return existente

    momento = momento or ahora_utc()
    documentos = documentos or proveedor_documentos()
    almacenamiento = almacenamiento or proveedor_almacenamiento()

    try:
        fila = _numerar_y_guardar(
            db,
            reserva,
            pago,
            emitido_en=momento,
            documentos=documentos,
            almacenamiento=almacenamiento,
        )
    except Exception as error:  # noqa: BLE001 - flow 1a: never lose the payment
        logger.warning("No se pudo emitir el comprobante del pago %s: %s", pago.id, error)
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_PAGO,
            pago.id,
            eventos.COMPROBANTE_NO_EMITIDO,
            autor_id=autor_id,
            datos={"reserva_id": reserva.id, "causa": str(error)[:300]},
        )
        return None

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_PAGO,
        pago.id,
        eventos.COMPROBANTE_EMITIDO,
        autor_id=autor_id,
        datos={
            "reserva_id": reserva.id,
            "comprobante_id": fila.id,
            "numero": fila.numero,
            "monto_centimos": fila.monto_centimos,
            "archivo_key": fila.archivo_key,
        },
    )

    # RF-027 step: "enviado por correo". A template and an event, exactly as
    # INC-5 asked - and flow 3a is already covered, because a failed delivery
    # is a row with its cause and the file stays where it is.
    notificacion_service.despachar(
        db,
        reserva.usuario,
        EventoNotificacion.COMPROBANTE.value,
        reserva=reserva,
        datos={
            "numero": fila.numero,
            "total": _soles(fila.monto_centimos, fila.moneda),
            "medio": fila.medio_pago,
        },
        momento=momento,
        **proveedores,
    )
    return fila


def de_reserva(db: Session, reserva: Reserva) -> Comprobante:
    """The latest receipt of a reservation (RF-027 CA-02)."""
    comprobantes = pago_repo.listar_comprobantes(db, reserva.id)
    if not comprobantes:
        raise ComprobanteNoDisponible(
            detalles=[detalle("pago", "Registra o completa el pago para emitir el comprobante.")]
        )
    return comprobantes[-1]


def obtener(db: Session, comprobante_id: int) -> Comprobante:
    fila = pago_repo.obtener_comprobante(db, comprobante_id)
    if fila is None:
        raise RecursoNoEncontrado("No encontramos ese comprobante.")
    return fila


def archivo(
    comprobante: Comprobante, *, almacenamiento: ProveedorAlmacenamiento | None = None
) -> bytes:
    """The stored PDF (RF-027 CA-02)."""
    almacenamiento = almacenamiento or proveedor_almacenamiento()
    try:
        return almacenamiento.leer(comprobante.archivo_key)
    except ObjetoNoEncontrado as error:
        raise RecursoNoEncontrado(
            "El archivo del comprobante no está disponible. "
            "Pide al administrador que vuelva a emitirlo.",
            detalles=[detalle("archivo_key", str(error))],
        ) from error
