"""Data access for the loyalty programme (RF-032, RN-11).

Queries only, as everywhere in this package. How many points a payment earns,
whether a balance is enough and which benefits deserve to be on the listing are
decisions, and decisions live in ``fidelizacion_service``.

The one thing worth pointing at is :func:`saldo`: the balance is a ``SUM`` over
``puntos_movimiento`` and never a stored counter, so it cannot drift away from
the movements that explain it.
"""

from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Beneficio, CuponCanje, PuntosMovimiento

#: How many statement lines ``GET /fidelizacion/saldo`` returns at most. The
#: balance is exact whatever this is - it is a ``SUM``, not a walk of the page.
LIMITE_MOVIMIENTOS = 50


# --------------------------------------------------------------------------
# beneficio
# --------------------------------------------------------------------------
def listar_beneficios(db: Session, fecha: date) -> list[Beneficio]:
    """Benefits a customer may still redeem on ``fecha`` (RF-032 flow 4b).

    "Beneficio agotado o vencido -> se retira del listado" is this ``WHERE``
    and nothing else: an exhausted or out-of-date row simply stops matching,
    exactly the way an expired promotion does (RF-011 flow 4a). Nothing sweeps
    and nothing deactivates, so a benefit whose stock is replenished or whose
    vigency is extended comes back on its own.
    """
    consulta = (
        select(Beneficio)
        .where(
            Beneficio.activo.is_(True),
            Beneficio.vigente_desde <= fecha,
            (Beneficio.vigente_hasta.is_(None)) | (Beneficio.vigente_hasta >= fecha),
            (Beneficio.stock.is_(None)) | (Beneficio.stock > 0),
        )
        .order_by(Beneficio.puntos_requeridos, Beneficio.id)
    )
    return list(db.scalars(consulta).all())


def obtener_beneficio(db: Session, beneficio_id: int) -> Beneficio | None:
    return db.get(Beneficio, beneficio_id)


def obtener_beneficio_por_nombre(db: Session, nombre: str) -> Beneficio | None:
    return db.scalars(select(Beneficio).where(Beneficio.nombre == nombre)).first()


# --------------------------------------------------------------------------
# puntos_movimiento
# --------------------------------------------------------------------------
def saldo(db: Session, usuario_id: int) -> int:
    """The customer's balance: the sum of their movements, nothing else."""
    total = db.scalar(
        select(func.coalesce(func.sum(PuntosMovimiento.puntos), 0)).where(
            PuntosMovimiento.usuario_id == usuario_id
        )
    )
    return int(total or 0)


def listar_movimientos(
    db: Session, usuario_id: int, *, limite: int = LIMITE_MOVIMIENTOS
) -> list[PuntosMovimiento]:
    """The statement, newest line first (RF-032 "saldo y movimientos")."""
    consulta = (
        select(PuntosMovimiento)
        .where(PuntosMovimiento.usuario_id == usuario_id)
        .order_by(PuntosMovimiento.ocurrido_en.desc(), PuntosMovimiento.id.desc())
        .limit(limite)
    )
    return list(db.scalars(consulta).all())


def obtener_movimiento_de_pago(db: Session, pago_id: int) -> PuntosMovimiento | None:
    """The accrual of one payment, if it was already credited.

    The guard behind ``uq_puntos_movimiento_pago``: the unique constraint is
    what makes double crediting impossible, this is what makes the second call
    quiet instead of an exception (RF-026 CA-02 replays a charge routinely).
    """
    consulta = select(PuntosMovimiento).where(PuntosMovimiento.pago_id == pago_id)
    return db.scalars(consulta).first()


def crear_movimiento(
    db: Session,
    *,
    usuario_id: int,
    tipo: str,
    puntos: int,
    saldo_resultante: int,
    ocurrido_en: datetime,
    reserva_id: int | None = None,
    pago_id: int | None = None,
    beneficio_id: int | None = None,
    base_centimos: int = 0,
) -> PuntosMovimiento:
    fila = PuntosMovimiento(
        usuario_id=usuario_id,
        tipo=tipo,
        puntos=puntos,
        saldo_resultante=saldo_resultante,
        reserva_id=reserva_id,
        pago_id=pago_id,
        beneficio_id=beneficio_id,
        base_centimos=base_centimos,
        ocurrido_en=ocurrido_en,
    )
    db.add(fila)
    db.flush()
    return fila


# --------------------------------------------------------------------------
# cupon_canje
# --------------------------------------------------------------------------
def obtener_cupon(db: Session, codigo: str) -> CuponCanje | None:
    """The redemption coupon behind a code, or ``None`` for an ordinary one."""
    return db.scalars(select(CuponCanje).where(CuponCanje.codigo == codigo)).first()


def obtener_cupon_por_promocion(db: Session, promocion_id: int) -> CuponCanje | None:
    """The redemption behind a promotion, or ``None`` when it is a plain one.

    This is what lets the tariff engine tell a marketing coupon (anybody may
    type it) from a redeemed one (it belongs to one customer and is spent once)
    without the two ever needing different code paths.
    """
    consulta = select(CuponCanje).where(CuponCanje.promocion_id == promocion_id)
    return db.scalars(consulta).first()


def existe_codigo_cupon(db: Session, codigo: str) -> bool:
    return db.scalars(select(CuponCanje.id).where(CuponCanje.codigo == codigo)).first() is not None


def listar_cupones(db: Session, usuario_id: int) -> list[CuponCanje]:
    """Every coupon this customer redeemed, newest first."""
    consulta = (
        select(CuponCanje)
        .where(CuponCanje.usuario_id == usuario_id)
        .order_by(CuponCanje.emitido_en.desc(), CuponCanje.id.desc())
    )
    return list(db.scalars(consulta).all())


def crear_cupon(
    db: Session,
    *,
    usuario_id: int,
    beneficio_id: int,
    codigo: str,
    promocion_id: int | None,
    estado: str,
    vence_en: datetime,
    emitido_en: datetime,
) -> CuponCanje:
    fila = CuponCanje(
        usuario_id=usuario_id,
        beneficio_id=beneficio_id,
        codigo=codigo,
        promocion_id=promocion_id,
        estado=estado,
        vence_en=vence_en,
        emitido_en=emitido_en,
    )
    db.add(fila)
    db.flush()
    return fila


def marcar_cupon_usado(
    db: Session, fila: CuponCanje, *, estado: str, reserva_id: int, usado_en: datetime
) -> CuponCanje:
    fila.estado = estado
    fila.reserva_id = reserva_id
    fila.usado_en = usado_en
    db.flush()
    return fila
