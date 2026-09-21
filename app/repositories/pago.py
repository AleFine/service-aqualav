"""Data access for the payment aggregate.

``pago`` and the three tables INC-4 hangs off it: the gateway ledger
(``transaccion_pasarela``), the receipts (``comprobante``) and the reversals
(``reembolso``). Queries only, no decisions: whether a refund fits in the
remaining balance, which state a payment moves to and what a refused reversal
means are all resolved one layer up.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    ESTADOS_PAGO_COBRADO,
    Comprobante,
    Pago,
    Reembolso,
    TransaccionPasarela,
)


def obtener_por_idempotency_key(db: Session, idempotency_key: str) -> Pago | None:
    """EXTENSION POINT P6: replaying a key must never create a second payment."""
    consulta = (
        select(Pago).where(Pago.idempotency_key == idempotency_key).options(joinedload(Pago.autor))
    )
    return db.scalars(consulta).first()


def obtener_por_id(db: Session, pago_id: int) -> Pago | None:
    consulta = select(Pago).where(Pago.id == pago_id).options(joinedload(Pago.autor))
    return db.scalars(consulta).first()


def obtener_confirmado(db: Session, reserva_id: int) -> Pago | None:
    """The payment that says this service was paid for (RN-09).

    ``reembolsado_parcial`` counts: the shop WAS paid, and giving part of it
    back afterwards does not turn the delivery of RF-024 CA-01 into an unpaid
    one. Which states count is declared once, in ``ESTADOS_PAGO_COBRADO``.
    """
    consulta = (
        select(Pago)
        .where(Pago.reserva_id == reserva_id, Pago.estado.in_(ESTADOS_PAGO_COBRADO))
        .order_by(Pago.registrado_en.desc(), Pago.id.desc())
        .options(joinedload(Pago.autor))
    )
    return db.scalars(consulta).first()


def listar_por_reserva(db: Session, reserva_id: int) -> list[Pago]:
    consulta = (
        select(Pago)
        .where(Pago.reserva_id == reserva_id)
        .order_by(Pago.registrado_en, Pago.id)
        .options(joinedload(Pago.autor))
    )
    return list(db.scalars(consulta).all())


def crear(
    db: Session,
    *,
    reserva_id: int,
    monto_centimos: int,
    moneda: str,
    medio: str,
    estado: str,
    idempotency_key: str,
    motivo_diferencia: str | None,
    autor_id: int,
    registrado_en: datetime,
    saldo_centimos: int | None = None,
    referencia_externa: str | None = None,
    pasarela: str | None = None,
    token_tarjeta: str | None = None,
    motivo_rechazo: str | None = None,
) -> Pago:
    if saldo_centimos is None:
        # RF-028: only money that actually arrived can be given back, so a
        # rejected or still unsettled charge starts with nothing to reverse.
        saldo_centimos = monto_centimos if estado in ESTADOS_PAGO_COBRADO else 0

    pago = Pago(
        reserva_id=reserva_id,
        monto_centimos=monto_centimos,
        saldo_centimos=saldo_centimos,
        moneda=moneda,
        medio=medio,
        estado=estado,
        idempotency_key=idempotency_key,
        referencia_externa=referencia_externa,
        pasarela=pasarela,
        token_tarjeta=token_tarjeta,
        motivo_rechazo=motivo_rechazo,
        motivo_diferencia=motivo_diferencia,
        autor_id=autor_id,
        registrado_en=registrado_en,
    )
    db.add(pago)
    db.flush()
    return pago


# --------------------------------------------------------------------------
# transaccion_pasarela (RF-026)
# --------------------------------------------------------------------------
def obtener_transaccion(db: Session, idempotency_key: str) -> TransaccionPasarela | None:
    """The gateway's own record for one key. RF-026 flow 3b reads it back."""
    consulta = select(TransaccionPasarela).where(
        TransaccionPasarela.idempotency_key == idempotency_key
    )
    return db.scalars(consulta).first()


def listar_transacciones(db: Session, reserva_id: int) -> list[TransaccionPasarela]:
    consulta = (
        select(TransaccionPasarela)
        .where(TransaccionPasarela.reserva_id == reserva_id)
        .order_by(TransaccionPasarela.id)
    )
    return list(db.scalars(consulta).all())


def crear_transaccion(
    db: Session,
    *,
    idempotency_key: str,
    reserva_id: int,
    operacion: str,
    estado: str,
    monto_centimos: int,
    moneda: str,
    referencia_externa: str | None,
    motivo: str | None,
    solicitud: dict,
    respuesta: dict,
) -> TransaccionPasarela:
    fila = TransaccionPasarela(
        idempotency_key=idempotency_key,
        reserva_id=reserva_id,
        operacion=operacion,
        estado=estado,
        monto_centimos=monto_centimos,
        moneda=moneda,
        referencia_externa=referencia_externa,
        motivo=motivo,
        solicitud=dict(solicitud),
        respuesta=dict(respuesta),
        intentos=1,
    )
    db.add(fila)
    db.flush()
    return fila


def marcar_transaccion_entregada(db: Session, fila: TransaccionPasarela) -> TransaccionPasarela:
    """The answer finally reached the caller (RF-026 flow 3b, after the query)."""
    respuesta = dict(fila.respuesta or {})
    respuesta["entregada"] = True
    fila.respuesta = respuesta
    fila.intentos = (fila.intentos or 0) + 1
    db.flush()
    return fila


def vincular_transaccion(
    db: Session, fila: TransaccionPasarela, pago_id: int
) -> TransaccionPasarela:
    """Attach the gateway call to the payment it ended up producing."""
    fila.pago_id = pago_id
    db.flush()
    return fila


# --------------------------------------------------------------------------
# comprobante (RF-027)
# --------------------------------------------------------------------------
def obtener_comprobante(db: Session, comprobante_id: int) -> Comprobante | None:
    consulta = select(Comprobante).where(Comprobante.id == comprobante_id)
    return db.scalars(consulta).first()


def obtener_comprobante_de_pago(db: Session, pago_id: int) -> Comprobante | None:
    consulta = select(Comprobante).where(Comprobante.pago_id == pago_id)
    return db.scalars(consulta).first()


def listar_comprobantes(db: Session, reserva_id: int) -> list[Comprobante]:
    consulta = (
        select(Comprobante).where(Comprobante.reserva_id == reserva_id).order_by(Comprobante.id)
    )
    return list(db.scalars(consulta).all())


def siguiente_correlativo(db: Session, serie: str) -> int:
    """The next number of the series (RF-027 CA-01).

    ``max + 1`` over a column with a unique constraint behind it: two callers
    racing produce one insert and one ``IntegrityError``, which the service
    retries. A sequence would be nicer and would also tie the numbering to one
    database engine, which RF-027 does not ask for.
    """
    maximo = db.scalar(
        select(func.max(Comprobante.numero_correlativo)).where(Comprobante.serie == serie)
    )
    return int(maximo or 0) + 1


def crear_comprobante(
    db: Session,
    *,
    reserva_id: int,
    pago_id: int,
    serie: str,
    numero_correlativo: int,
    monto_centimos: int,
    moneda: str,
    medio_pago: str,
    archivo_key: str,
    emitido_en: datetime,
) -> Comprobante:
    fila = Comprobante(
        reserva_id=reserva_id,
        pago_id=pago_id,
        serie=serie,
        numero_correlativo=numero_correlativo,
        monto_centimos=monto_centimos,
        moneda=moneda,
        medio_pago=medio_pago,
        archivo_key=archivo_key,
        emitido_en=emitido_en,
    )
    db.add(fila)
    db.flush()
    return fila


# --------------------------------------------------------------------------
# reembolso (RF-028)
# --------------------------------------------------------------------------
def obtener_reembolso_por_idempotency_key(db: Session, idempotency_key: str) -> Reembolso | None:
    """RNF-017 M1: replaying a refund key must never give the money twice."""
    consulta = (
        select(Reembolso)
        .where(Reembolso.idempotency_key == idempotency_key)
        .options(joinedload(Reembolso.autor))
    )
    return db.scalars(consulta).first()


def listar_reembolsos(db: Session, pago_id: int) -> list[Reembolso]:
    consulta = (
        select(Reembolso)
        .where(Reembolso.pago_id == pago_id)
        .order_by(Reembolso.id)
        .options(joinedload(Reembolso.autor))
    )
    return list(db.scalars(consulta).all())


def crear_reembolso(
    db: Session,
    *,
    pago_id: int,
    tipo: str,
    monto_centimos: int,
    moneda: str,
    motivo: str,
    estado: str,
    referencia_externa: str | None,
    detalle: str | None,
    idempotency_key: str,
    autor_id: int | None,
    registrado_en: datetime,
) -> Reembolso:
    fila = Reembolso(
        pago_id=pago_id,
        tipo=tipo,
        monto_centimos=monto_centimos,
        moneda=moneda,
        motivo=motivo,
        estado=estado,
        referencia_externa=referencia_externa,
        detalle=detalle,
        idempotency_key=idempotency_key,
        autor_id=autor_id,
        registrado_en=registrado_en,
    )
    db.add(fila)
    db.flush()
    return fila


def actualizar_saldo(db: Session, pago: Pago, *, saldo_centimos: int, estado: str) -> Pago:
    """Write back what is left of a payment after a reversal (RF-028 CA-01)."""
    pago.saldo_centimos = saldo_centimos
    pago.estado = estado
    db.flush()
    return pago
