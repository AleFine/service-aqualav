"""Data access for ``pago``."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import EstadoPago, Pago


def obtener_por_idempotency_key(db: Session, idempotency_key: str) -> Pago | None:
    """EXTENSION POINT P6: replaying a key must never create a second payment."""
    consulta = (
        select(Pago).where(Pago.idempotency_key == idempotency_key).options(joinedload(Pago.autor))
    )
    return db.scalars(consulta).first()


def obtener_confirmado(db: Session, reserva_id: int) -> Pago | None:
    """The confirmed payment of a reservation, if any (RN-09)."""
    consulta = (
        select(Pago)
        .where(Pago.reserva_id == reserva_id, Pago.estado == EstadoPago.CONFIRMADO.value)
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
) -> Pago:
    pago = Pago(
        reserva_id=reserva_id,
        monto_centimos=monto_centimos,
        moneda=moneda,
        medio=medio,
        estado=estado,
        idempotency_key=idempotency_key,
        motivo_diferencia=motivo_diferencia,
        autor_id=autor_id,
        registrado_en=registrado_en,
    )
    db.add(pago)
    db.flush()
    return pago
