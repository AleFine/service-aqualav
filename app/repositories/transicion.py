"""Data access for ``transicion_estado`` (EXTENSION POINT P3).

The state machine lives in this table, never in code: RF-021 CA-03 requires a
new row to become usable after an API restart with no deploy.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TransicionEstado


def listar_todas(db: Session) -> list[TransicionEstado]:
    """Every declared move, in insertion order.

    The row order is what makes any ordering derived from this table stable
    across calls, which the state catalogue relies on.
    """
    return list(db.scalars(select(TransicionEstado).order_by(TransicionEstado.id)).all())


def listar_por_origen(db: Session, estado_origen: str) -> list[TransicionEstado]:
    """Every declared move leaving one state."""
    consulta = (
        select(TransicionEstado)
        .where(TransicionEstado.estado_origen == estado_origen)
        .order_by(TransicionEstado.id)
    )
    return list(db.scalars(consulta).all())


def obtener(db: Session, estado_origen: str, estado_destino: str) -> TransicionEstado | None:
    """The row declaring one move, or ``None`` when the move is not allowed."""
    consulta = select(TransicionEstado).where(
        TransicionEstado.estado_origen == estado_origen,
        TransicionEstado.estado_destino == estado_destino,
    )
    return db.scalars(consulta).first()


def listar_estados_no_terminales(db: Session) -> set[str]:
    """States with at least one declared outgoing move.

    A reservation sitting in one of these is still being worked on, so it holds
    its bay (RN-03) and the counter can still find it (RF-019). Derived from the
    table and never listed in code (P3): a state added as data occupies its bay
    and shows up in the staff search with no code change, and ``finalizado``
    keeps its bay until the delivery is registered (RF-024 CA-02).
    """
    consulta = select(TransicionEstado.estado_origen).distinct()
    return set(db.scalars(consulta).all())
