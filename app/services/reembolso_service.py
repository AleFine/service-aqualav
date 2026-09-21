"""Annulments and refunds (RF-028).

One operation, :func:`solicitar`, and the three outcomes the requirement
describes:

* the gateway reverses the charge -> ``procesado``, and the payment's balance
  drops by exactly that amount (CA-01);
* more than the balance is asked for -> **422** and nothing happens (CA-02);
* the gateway refuses the reversal -> the request "queda registrada como
  pendiente de gestión manual" (flow 3a). It is NOT an error the caller has to
  retry and it is NOT silently dropped: the row exists, in
  ``pendiente_manual``, with the gateway's reason on it, and somebody finishes
  it by hand.

``RNF-017`` M1 asks for the idempotency key to be mandatory **here too**, so
the same discipline the charge has applies: the key is unique on ``reembolso``,
replaying it returns the row that already exists and never moves money twice.

A payment taken at the counter has no gateway to ask. Giving cash back is the
cashier handing it over, so the reversal is recorded as ``procesado`` with no
external reference; the audit trail says which of the two it was.
"""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.errors import (
    DatosInvalidos,
    IdempotencyKeyRequerida,
    MontoMayorAlPagado,
    RecursoNoEncontrado,
    detalle,
)
from app.core.horario import ahora_utc
from app.models import (
    EstadoPago,
    EstadoReembolso,
    EventoNotificacion,
    Pago,
    Reembolso,
    TipoReembolso,
    Usuario,
)
from app.repositories import pago as pago_repo
from app.services import eventos, notificacion_service
from app.services.proveedores.pasarela import (
    PasarelaNoDisponible,
    ProveedorPasarela,
    TiempoDeEsperaAgotado,
    proveedor_pasarela,
)

logger = logging.getLogger("aqualav.reembolsos")

#: Refund types that always take the whole remaining balance.
TIPOS_TOTALES = frozenset({TipoReembolso.ANULACION.value, TipoReembolso.TOTAL.value})

#: What the audit trail says when there was no gateway to ask.
DETALLE_SIN_PASARELA = "Devolución registrada en caja: el cobro no pasó por la pasarela."


def resolver_clave(idempotency_key: str | None, del_cuerpo: str | None = None) -> str:
    """RNF-017 M1: the header wins, the body is the documented fallback."""
    clave = (idempotency_key or del_cuerpo or "").strip()
    if not clave:
        raise IdempotencyKeyRequerida(
            "Falta la cabecera Idempotency-Key para registrar el reembolso. "
            "Reintenta la operación enviando una clave única.",
            detalles=[detalle("Idempotency-Key", "Envía una clave única por cada reembolso.")],
        )
    return clave[:80]


def obtener_pago(db: Session, pago_id: int) -> Pago:
    pago = pago_repo.obtener_por_id(db, pago_id)
    if pago is None:
        raise RecursoNoEncontrado("No encontramos ese pago.")
    return pago


def _monto_solicitado(pago: Pago, tipo: str, monto_centimos: int | None) -> int:
    """How much this request is really asking for, validated against the balance."""
    if tipo in TIPOS_TOTALES:
        monto = pago.saldo_centimos
    else:
        monto = int(monto_centimos or 0)

    if monto <= 0:
        raise DatosInvalidos(
            "No queda saldo por devolver en ese pago. "
            "Revisa los reembolsos ya registrados antes de solicitar otro.",
            detalles=[detalle("monto_centimos", f"Saldo disponible: {pago.saldo_centimos}.")],
        )
    if monto > pago.saldo_centimos:
        # RF-028 CA-02 / flow 2a.
        raise MontoMayorAlPagado(
            detalles=[
                detalle("monto_centimos", f"Saldo disponible: {pago.saldo_centimos}."),
                detalle("monto_centimos", f"Monto solicitado: {monto}."),
            ]
        )
    return monto


def _estado_resultante(pago: Pago, tipo: str, saldo: int) -> str:
    """What the payment becomes once this reversal is applied."""
    if tipo == TipoReembolso.ANULACION.value:
        return EstadoPago.ANULADO.value
    if saldo <= 0:
        return EstadoPago.REEMBOLSADO_TOTAL.value
    return EstadoPago.REEMBOLSADO_PARCIAL.value


def _pedir_reversion(
    db: Session,
    pago: Pago,
    *,
    clave: str,
    monto: int,
    motivo: str,
    pasarela: ProveedorPasarela | None,
) -> tuple[str, str | None, str | None]:
    """Ask the gateway to reverse, and translate its answer.

    Returns ``(estado del reembolso, referencia externa, detalle)``. Every
    failure mode of the gateway - a refusal, an outage, a lost reply - lands on
    ``pendiente_manual``, because from the customer's point of view they are
    the same thing: the money has not come back and a person has to chase it.
    """
    if not pago.pasarela:
        return EstadoReembolso.PROCESADO.value, None, DETALLE_SIN_PASARELA

    pasarela = pasarela or proveedor_pasarela(sesion=db)
    try:
        resultado = pasarela.reembolsar(
            idempotency_key=clave,
            reserva_id=pago.reserva_id,
            token_tarjeta=pago.token_tarjeta,
            monto_centimos=monto,
            moneda=pago.moneda,
            motivo=motivo,
            referencia_original=pago.referencia_externa,
        )
    except (PasarelaNoDisponible, TiempoDeEsperaAgotado) as error:
        logger.info("reversion sin respuesta pago=%s causa=%s", pago.id, error)
        return EstadoReembolso.PENDIENTE_MANUAL.value, None, str(error)[:300]

    if not resultado.aprobada:
        return (
            EstadoReembolso.PENDIENTE_MANUAL.value,
            resultado.referencia_externa,
            (resultado.motivo or "La pasarela rechazó la reversión.")[:300],
        )
    return EstadoReembolso.PROCESADO.value, resultado.referencia_externa, None


def solicitar(
    db: Session,
    pago: Pago,
    *,
    tipo: str,
    motivo: str,
    idempotency_key: str,
    monto_centimos: int | None = None,
    autor: Usuario | None = None,
    momento: datetime | None = None,
    pasarela: ProveedorPasarela | None = None,
    confirmar: bool = True,
    **proveedores,
) -> tuple[Reembolso, bool]:
    """Reverse part or all of a payment (RF-028).

    Returns ``(reembolso, creado)``; ``creado`` is ``False`` when the
    idempotency key had already been used, which is what lets the endpoint
    answer 200 instead of 201 without giving the money back twice.
    """
    existente = pago_repo.obtener_reembolso_por_idempotency_key(db, idempotency_key)
    if existente is not None:
        return existente, False

    momento = momento or ahora_utc()
    monto = _monto_solicitado(pago, tipo, monto_centimos)
    estado, referencia, detalle_gestion = _pedir_reversion(
        db, pago, clave=idempotency_key, monto=monto, motivo=motivo, pasarela=pasarela
    )

    fila = pago_repo.crear_reembolso(
        db,
        pago_id=pago.id,
        tipo=tipo,
        monto_centimos=monto,
        moneda=pago.moneda,
        motivo=motivo,
        estado=estado,
        referencia_externa=referencia,
        detalle=detalle_gestion,
        idempotency_key=idempotency_key,
        autor_id=autor.id if autor is not None else None,
        registrado_en=momento,
    )

    procesado = estado == EstadoReembolso.PROCESADO.value
    if procesado:
        # CA-01: "el saldo del pago se reduce en ese monto", and nothing else.
        saldo = pago.saldo_centimos - monto
        pago_repo.actualizar_saldo(
            db, pago, saldo_centimos=saldo, estado=_estado_resultante(pago, tipo, saldo)
        )

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_REEMBOLSO,
        fila.id,
        eventos.REEMBOLSO_PROCESADO if procesado else eventos.REEMBOLSO_PENDIENTE_MANUAL,
        autor_id=autor.id if autor is not None else None,
        datos={
            "pago_id": pago.id,
            "reserva_id": pago.reserva_id,
            "tipo": tipo,
            "monto_centimos": monto,
            "saldo_centimos": pago.saldo_centimos,
            "estado_pago": pago.estado,
            "referencia_externa": referencia,
            "detalle": detalle_gestion,
        },
    )

    # RF-028 output: "cliente notificado". A template and an event (INC-5).
    notificacion_service.despachar(
        db,
        pago.reserva.usuario,
        EventoNotificacion.REEMBOLSO.value,
        reserva=pago.reserva,
        datos={
            "monto": f"{pago.moneda} {monto // 100}.{monto % 100:02d}",
            "estado_reembolso": estado,
            "motivo_reembolso": motivo,
        },
        momento=momento,
        **proveedores,
    )

    if confirmar:
        db.commit()
        db.refresh(fila)
    return fila, True


def listar(db: Session, pago: Pago) -> list[Reembolso]:
    """Every reversal of one payment, including the ones awaiting a human."""
    return pago_repo.listar_reembolsos(db, pago.id)
