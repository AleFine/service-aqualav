"""Data access for ``evento_dominio`` (EXTENSION POINT P7).

Nothing in the MVP reads this table. That is intentional: it is the seed of the
future audit trail and of the notification feed.
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


def listar_por_entidad(db: Session, entidad: str, entidad_id: int) -> list[EventoDominio]:
    """Only the test suite and future reporting read the log back."""
    consulta = (
        select(EventoDominio)
        .where(EventoDominio.entidad == entidad, EventoDominio.entidad_id == entidad_id)
        .order_by(EventoDominio.ocurrido_en, EventoDominio.id)
    )
    return list(db.scalars(consulta).all())
