"""Data access for ``evento_dominio`` (EXTENSION POINT P7).

Nothing in the MVP read this table. INC-6 is the first consumer that does:
RN-10 needs to know WHEN the rating window opened, and that instant only
exists here (see :func:`obtener_ultimo`). RF-036 will expose the rest of it.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EventoDominio


def crear(
    db: Session,
    *,
    entidad: str,
    entidad_id: int,
    accion: str,
    autor_id: int | None,
    datos: dict[str, Any],
) -> EventoDominio:
    evento = EventoDominio(
        entidad=entidad,
        entidad_id=entidad_id,
        accion=accion,
        autor_id=autor_id,
        datos=datos,
    )
    db.add(evento)
    db.flush()
    return evento


def obtener_ultimo(db: Session, entidad: str, entidad_id: int, accion: str) -> EventoDominio | None:
    """The most recent event of that kind, or ``None``.

    INC-6 is the first consumer: RN-10 counts its seven calendar days from the
    ``reserva.calificacion_habilitada`` row the check-out wrote, because the
    moment the window opened cannot be reconstructed from anything else once
    the assignment has been released.
    """
    consulta = (
        select(EventoDominio)
        .where(
            EventoDominio.entidad == entidad,
            EventoDominio.entidad_id == entidad_id,
            EventoDominio.accion == accion,
        )
        .order_by(EventoDominio.ocurrido_en.desc(), EventoDominio.id.desc())
    )
    return db.scalars(consulta).first()


def listar_por_entidad(db: Session, entidad: str, entidad_id: int) -> list[EventoDominio]:
    """Only the test suite and future reporting read the log back."""
    consulta = (
        select(EventoDominio)
        .where(EventoDominio.entidad == entidad, EventoDominio.entidad_id == entidad_id)
        .order_by(EventoDominio.ocurrido_en, EventoDominio.id)
    )
    return list(db.scalars(consulta).all())
