"""Payments: the counter charge, the gateway charge and the modality (RF-025, RF-026).

EXTENSION POINT P6 paid off exactly as it was meant to: the idempotency key has
been mandatory since the MVP, when nothing retried, and it is now the single
thing that keeps RF-026 flow 3b from charging a customer twice. Not one payment
row had to be migrated to get here.

Three operations live in this module and they share one rule - **the money
decides, not the state name**:

* :func:`registrar` is the counter charge of the MVP. Unchanged, except that a
  confirmed payment now issues its receipt (RF-027) and credits the loyalty
  points of RN-11 (RF-032: "acumulación al confirmarse el pago");
* :func:`cobrar_en_linea` is the gateway charge of RF-026 v1.0. It records the
  external identifier, reports a rejection with its reason (flow 3a) and, when
  the answer never arrives, **asks for the state with the same idempotency key
  before even considering a retry** (flow 3b);
* :func:`cambiar_modalidad` is RF-025 flow 4a: while the payment is not
  confirmed the customer may move between paying online and paying at the shop.

Where a successful online charge LEAVES the reservation is not written here.
It is read from ``transicion_estado`` through
``operacion_service.destino_declarado`` (P3), exactly like the check-in.

RNF-013 M3: the card number reaches :func:`cobrar_en_linea` and is turned into
a token in its first lines. Nothing downstream - not the payment row, not the
gateway ledger, not the event log - ever sees it again.
"""

from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import (
    DatosInvalidos,
    IdempotencyKeyRequerida,
    ModalidadDePagoNoModificable,
    PagoRechazado,
    PasarelaNoDisponible,
    detalle,
)
from app.core.horario import ahora_utc
from app.models import (
    EstadoPago,
    EstadoTransaccion,
    ModalidadPago,
    Pago,
    Reserva,
    Usuario,
)
from app.repositories import pago as pago_repo
from app.schemas import PagoCrear, PagoEnLineaCrear
from app.services import comprobante_service, eventos, fidelizacion_service, operacion_service
from app.services.proveedores import pasarela as pasarela_mod
from app.services.proveedores.pasarela import ProveedorPasarela, proveedor_pasarela


def ventana_de_pago() -> timedelta:
    """How long an online booking holds its block (RF-014 flow 2a).

    Read from the setting on every call so a demo can shorten the window
    without restarting the API.
    """
    return timedelta(minutes=settings.pago_en_linea_ventana_minutos)


#: What the gateway answered -> what the payment row becomes.
ESTADO_DE_PAGO_POR_RESULTADO: dict[str, str] = {
    EstadoTransaccion.APROBADA.value: EstadoPago.CONFIRMADO.value,
    EstadoTransaccion.PENDIENTE.value: EstadoPago.PENDIENTE.value,
    EstadoTransaccion.RECHAZADA.value: EstadoPago.RECHAZADO.value,
}

#: What the gateway answered -> which domain event it writes.
EVENTO_POR_RESULTADO: dict[str, str] = {
    EstadoTransaccion.APROBADA.value: eventos.PAGO_EN_LINEA_APROBADO,
    EstadoTransaccion.PENDIENTE.value: eventos.PAGO_EN_LINEA_PENDIENTE,
    EstadoTransaccion.RECHAZADA.value: eventos.PAGO_EN_LINEA_RECHAZADO,
}


def _resolver_clave(datos: PagoCrear | PagoEnLineaCrear, idempotency_key: str | None) -> str:
    """The header wins; the body field is the documented fallback."""
    clave = (idempotency_key or datos.idempotency_key or "").strip()
    if not clave:
        raise IdempotencyKeyRequerida(
            detalles=[detalle("Idempotency-Key", "Envía una clave única por cada cobro.")]
        )
    return clave[:80]


def registrar(
    db: Session,
    reserva: Reserva,
    datos: PagoCrear,
    autor: Usuario,
    idempotency_key: str | None,
    **proveedores,
) -> tuple[Pago, bool]:
    """Register a payment, or replay the existing one.

    Returns ``(pago, creado)``. ``creado`` is ``False`` when the key had
    already been used, which is what makes the endpoint answer 200 instead of
    201 without inserting a second row (CA-02).
    """
    clave = _resolver_clave(datos, idempotency_key)

    existente = pago_repo.obtener_por_idempotency_key(db, clave)
    if existente is not None:
        return existente, False

    if reserva.hora_fin_real is None:
        # "The service is over" is a FACT the state machine recorded, not a
        # state name: ``cambiar_estado`` stamps ``hora_fin_real`` when the move
        # it performed carries ``marca_fin_servicio``. Charging therefore keeps
        # working when v0.4 inserts a state between finishing and delivering.
        raise DatosInvalidos(
            "Solo se puede cobrar un servicio terminado. "
            "Avanza el estado antes de registrar el pago.",
            detalles=[detalle("estado", f"Estado actual: «{reserva.estado}».")],
        )

    if datos.monto_centimos != reserva.monto_centimos and not datos.motivo_diferencia:
        # RF-026 flow 3a: a different amount always has to be justified.
        raise DatosInvalidos(
            "El monto cobrado no coincide con el de la reserva. Indica el motivo de la diferencia.",
            detalles=[
                detalle("motivo_diferencia", "Obligatorio cuando el monto difiere del esperado."),
                detalle("monto_centimos", f"Monto esperado: {reserva.monto_centimos}."),
            ],
        )

    try:
        pago = pago_repo.crear(
            db,
            reserva_id=reserva.id,
            monto_centimos=datos.monto_centimos,
            moneda=reserva.moneda,
            medio=datos.medio.value,
            estado=EstadoPago.CONFIRMADO.value,
            idempotency_key=clave,
            motivo_diferencia=datos.motivo_diferencia,
            autor_id=autor.id,
            registrado_en=ahora_utc(),
        )
    except IntegrityError:
        # Two requests raced with the same key: the loser replays the winner.
        db.rollback()
        existente = pago_repo.obtener_por_idempotency_key(db, clave)
        if existente is None:  # pragma: no cover - only a genuine constraint bug
            raise
        return existente, False

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_PAGO,
        pago.id,
        eventos.PAGO_REGISTRADO,
        autor_id=autor.id,
        datos={
            "reserva_id": reserva.id,
            "monto_centimos": pago.monto_centimos,
            "medio": pago.medio,
            "idempotency_key": clave,
        },
    )

    # RF-027 precondition: "el pago del servicio se encuentra confirmado". It
    # is the same door for the counter and for the gateway, which is why
    # RF-024's delta ("el sistema envía el comprobante") needed no change in
    # the check-out either.
    comprobante_service.emitir(
        db, reserva, pago, autor_id=autor.id, momento=pago.registrado_en, **proveedores
    )
    # RF-032 / RN-11: "acumulación AL CONFIRMARSE EL PAGO". Same door as the
    # receipt, for the same reason - it is one moment however the money came
    # in - and idempotent by ``pago_id``, so a replayed key credits once.
    fidelizacion_service.acreditar(db, reserva, pago, momento=pago.registrado_en)

    db.commit()
    db.refresh(pago)
    return pago, True


# --------------------------------------------------------------------------
# RF-026 v1.0 - the gateway charge
# --------------------------------------------------------------------------
def _cobro_de_la_pasarela(
    db: Session,
    reserva: Reserva,
    *,
    clave: str,
    token: str,
    pasarela: ProveedorPasarela,
) -> tuple[pasarela_mod.ResultadoPasarela, bool]:
    """Charge, and if the answer is lost, ASK before retrying (flow 3b).

    Returns ``(resultado, consultada)``. ``consultada`` is what the test of
    flow 3b asserts on: it means the outcome came from
    :meth:`ProveedorPasarela.consultar` with the SAME idempotency key, not from
    a second charge - which is the difference between one payment and two.
    """
    try:
        return (
            pasarela.cobrar(
                idempotency_key=clave,
                reserva_id=reserva.id,
                monto_centimos=reserva.monto_centimos,
                moneda=reserva.moneda,
                token_tarjeta=token,
                descripcion=f"Reserva {reserva.codigo}",
            ),
            False,
        )
    except pasarela_mod.TiempoDeEsperaAgotado:
        consultado = pasarela.consultar(clave)
        if consultado is None:
            # The gateway does not know the key either, so nothing was charged
            # and nothing may be assumed. Offer the counter (RF-025 flow 3a).
            raise PasarelaNoDisponible(
                "La pasarela no respondió y tampoco pudo confirmarnos el estado del cobro. "
                "Vuelve a intentarlo en unos minutos o continúa con pago presencial.",
                detalles=[detalle("idempotency_key", "Reutiliza esta misma clave al reintentar.")],
            ) from None
        return consultado, True
    except pasarela_mod.PasarelaNoDisponible as error:
        raise PasarelaNoDisponible(detalles=[detalle("modalidad_pago", str(error))]) from error


def cobrar_en_linea(
    db: Session,
    reserva: Reserva,
    datos: PagoEnLineaCrear,
    autor: Usuario,
    permisos: list[str],
    idempotency_key: str | None,
    *,
    pasarela: ProveedorPasarela | None = None,
    **proveedores,
) -> tuple[Pago, bool]:
    """Charge a reservation through the gateway (RF-026 v1.0).

    Returns ``(pago, creado)``. ``creado`` is ``False`` when the idempotency
    key was replayed, which is the one case where nothing at all happens.

    Raises :class:`PasarelaNoDisponible` (503) when the gateway is unreachable,
    so the caller can offer the presential alternative of RF-025 flow 3a, and
    :class:`PagoRechazado` (422) when it refuses the card - after RECORDING the
    rejected payment, so flow 3a's "reintentar con otro medio" is a retry with
    a new key and not a hole in the history.
    """
    clave = _resolver_clave(datos, idempotency_key)

    existente = pago_repo.obtener_por_idempotency_key(db, clave)
    if existente is not None:
        return existente, False

    pasarela = pasarela or proveedor_pasarela(sesion=db)
    if not pasarela.disponible():
        # RF-025 flow 3a / CA-02. The reservation is NOT touched: the block is
        # still held and the customer only has to switch modality.
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_RESERVA,
            reserva.id,
            eventos.PAGO_PASARELA_NO_DISPONIBLE,
            autor_id=autor.id,
            datos={
                "idempotency_key": clave,
                "modalidad_alternativa": ModalidadPago.PRESENCIAL.value,
            },
        )
        db.commit()
        raise PasarelaNoDisponible(
            detalles=[
                detalle(
                    "modalidad_pago",
                    "Cambia la modalidad a «presencial» para conservar la reserva y pagar "
                    "en el local.",
                )
            ]
        )

    # RNF-013 M3: this is the last line where a card number exists.
    token = pasarela_mod.tokenizar(datos.numero_tarjeta)
    resultado, consultada = _cobro_de_la_pasarela(
        db, reserva, clave=clave, token=token, pasarela=pasarela
    )

    estado_pago = ESTADO_DE_PAGO_POR_RESULTADO.get(resultado.estado, EstadoPago.PENDIENTE.value)
    pago = pago_repo.crear(
        db,
        reserva_id=reserva.id,
        monto_centimos=reserva.monto_centimos,
        moneda=reserva.moneda,
        medio=datos.medio.value,
        estado=estado_pago,
        idempotency_key=clave,
        motivo_diferencia=None,
        autor_id=autor.id,
        registrado_en=ahora_utc(),
        referencia_externa=resultado.referencia_externa,
        pasarela=getattr(pasarela, "nombre", settings.pasarela_nombre),
        token_tarjeta=token,
        motivo_rechazo=resultado.motivo if resultado.rechazada else None,
    )

    transaccion = pago_repo.obtener_transaccion(db, clave)
    if transaccion is not None:
        pago_repo.vincular_transaccion(db, transaccion, pago.id)

    if consultada:
        # RF-026 flow 3b, written down so the audit can prove it happened.
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_PAGO,
            pago.id,
            eventos.PAGO_EN_LINEA_CONSULTADO,
            autor_id=autor.id,
            datos={"idempotency_key": clave, "estado_pasarela": resultado.estado},
        )

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_PAGO,
        pago.id,
        EVENTO_POR_RESULTADO.get(resultado.estado, eventos.PAGO_EN_LINEA_PENDIENTE),
        autor_id=autor.id,
        datos={
            "reserva_id": reserva.id,
            "monto_centimos": pago.monto_centimos,
            "medio": pago.medio,
            "idempotency_key": clave,
            # Step 4: "registrar la transacción con su identificador externo".
            "referencia_externa": resultado.referencia_externa,
            "token_tarjeta": token,
            "motivo": resultado.motivo,
        },
    )

    if resultado.aprobada:
        _confirmar_reserva_pagada(db, reserva, pago, autor, permisos, **proveedores)
        comprobante_service.emitir(
            db, reserva, pago, autor_id=autor.id, momento=pago.registrado_en, **proveedores
        )
        # RF-032 / RN-11: only an APPROVED charge earns points. A rejected or
        # unsettled one leaves the reservation waiting, so there is nothing
        # billed yet to reward.
        fidelizacion_service.acreditar(db, reserva, pago, momento=pago.registrado_en)
        db.commit()
        db.refresh(pago)
        return pago, True

    # Rejected or still unsettled: the reservation keeps waiting, and the
    # attempt is on the record either way. The commit comes BEFORE the error so
    # flow 3a can report a motive that somebody can look up afterwards.
    db.commit()
    db.refresh(pago)

    if resultado.rechazada:
        raise PagoRechazado(
            detalles=[
                detalle("medio", resultado.motivo or "La pasarela no autorizó la operación."),
                detalle("pago_id", str(pago.id)),
            ]
        )
    return pago, True


def _confirmar_reserva_pagada(
    db: Session,
    reserva: Reserva,
    pago: Pago,
    autor: Usuario,
    permisos: list[str],
    **proveedores,
) -> None:
    """Move the reservation out of "waiting for the money", if it was there.

    WHERE it lands is the row in ``transicion_estado`` (P3), never a literal.
    A reservation that was already confirmed - somebody who chose the counter
    and then paid online anyway (RF-025 flow 4a) - declares no move owned by
    this operation, so nothing happens and the payment simply attaches to it.

    ``evento_notificacion`` on that row is ``confirmacion``, so the customer is
    told by the table and not by a dispatch written here (INC-5).
    """
    if not operacion_service.tiene_operacion_declarada(
        db, reserva, operacion_service.ENDPOINT_PAGO
    ):
        reserva.expira_en = None
        return

    destino = operacion_service.destino_declarado(db, reserva, operacion_service.ENDPOINT_PAGO)
    operacion_service.cambiar_estado(
        db,
        reserva,
        destino,
        autor,
        permisos,
        origen_llamada=operacion_service.ENDPOINT_PAGO,
        datos={"pago_id": pago.id, "referencia_externa": pago.referencia_externa},
        confirmar=False,
        **proveedores,
    )
    # The block is paid for: it no longer expires (RF-014 flow 2a).
    reserva.expira_en = None


# --------------------------------------------------------------------------
# RF-025 - choosing and changing the modality
# --------------------------------------------------------------------------
def cambiar_modalidad(
    db: Session,
    reserva: Reserva,
    modalidad: str,
    autor: Usuario,
    permisos: list[str],
    **proveedores,
) -> Reserva:
    """Move the booking between online and counter payment (RF-025 flow 4a).

    "El cliente puede cambiar de modalidad mientras el pago no esté
    confirmado": a confirmed payment is refused with 422, because what that
    customer wants is a refund (RF-028) and not a different modality.

    Choosing ``presencial`` on a booking that was waiting for the gateway
    CONFIRMS it - RF-025 CA-01, "cuando se confirma la reserva, entonces su
    estado de pago es Pendiente" - through the move the table declares for the
    payment operation, so the confirmation notice goes out by itself.

    Choosing ``en_linea`` on an already confirmed booking only changes the
    modality: the block is held, so there is nothing to expire and no state to
    walk back. Annex A has no way back into "pendiente de pago" and inventing
    one would mean a confirmed customer could lose their slot to a timer.
    """
    if pago_repo.obtener_confirmado(db, reserva.id) is not None:
        raise ModalidadDePagoNoModificable(
            detalles=[detalle("modalidad_pago", "El pago de la reserva ya está confirmado.")]
        )

    anterior = reserva.modalidad_pago
    reserva.modalidad_pago = modalidad

    if modalidad == ModalidadPago.PRESENCIAL.value and operacion_service.tiene_operacion_declarada(
        db, reserva, operacion_service.ENDPOINT_PAGO
    ):
        destino = operacion_service.destino_declarado(db, reserva, operacion_service.ENDPOINT_PAGO)
        operacion_service.cambiar_estado(
            db,
            reserva,
            destino,
            autor,
            permisos,
            origen_llamada=operacion_service.ENDPOINT_PAGO,
            datos={"modalidad_pago": modalidad, "modalidad_anterior": anterior},
            confirmar=False,
            **proveedores,
        )
        reserva.expira_en = None

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        eventos.RESERVA_MODALIDAD_PAGO_CAMBIADA,
        autor_id=autor.id,
        datos={"anterior": anterior, "nueva": modalidad, "estado": reserva.estado},
    )
    db.commit()
    db.refresh(reserva)
    return reserva
