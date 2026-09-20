"""Data access for ``bahia``."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Bahia


def listar_activas(db: Session) -> list[Bahia]:
    """Active bays, lowest id first (the assignment order of RF-014)."""
    consulta = select(Bahia).where(Bahia.activa.is_(True)).order_by(Bahia.id)
    return list(db.scalars(consulta).all())


def listar_activas_bloqueadas(db: Session) -> list[Bahia]:
    """Same set, locked with ``FOR UPDATE`` for the duration of the transaction.

    This is the serialization point of RF-014 CA-02: two simultaneous requests
    for the same block queue on this lock, so exactly one of them finds a free
    bay. SQLite ignores ``FOR UPDATE`` (it has no such clause); PostgreSQL,
    which is what runs in Docker, honours it.
    """
    consulta = select(Bahia).where(Bahia.activa.is_(True)).order_by(Bahia.id).with_for_update()
    return list(db.scalars(consulta).all())


def contar_activas(db: Session) -> int:
    return len(listar_activas(db))


def listar_todas(db: Session) -> list[Bahia]:
    """Every bay, active or not: the administration screen sees them all."""
    return list(db.scalars(select(Bahia).order_by(Bahia.id)).all())


def obtener_por_id(db: Session, bahia_id: int) -> Bahia | None:
    return db.scalars(select(Bahia).where(Bahia.id == bahia_id)).first()


def obtener_por_nombre(db: Session, nombre: str) -> Bahia | None:
    return db.scalars(select(Bahia).where(Bahia.nombre == nombre)).first()


def crear(db: Session, *, nombre: str, activa: bool, estado: str) -> Bahia:
    bahia = Bahia(nombre=nombre, activa=activa, estado=estado)
    db.add(bahia)
    db.flush()
    return bahia
