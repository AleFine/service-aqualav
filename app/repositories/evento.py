"""Data access for ``evento_dominio`` (EXTENSION POINT P7, RF-036).

Nothing in the MVP read this table. INC-6 was the first consumer that did:
RN-10 needs to know WHEN the rating window opened, and that instant only
exists here (see :func:`obtener_ultimo`). INC-8 reads the rest of it, which is
what the table was written for.

**There is no update and no delete in this module, and there is none anywhere
else either.** RF-036 CA-02 ("cuando se intenta editar un registro por la API,
la operación no existe o se deniega") and RNF-014 ("solo inserción") are
enforced by absence: the audit trail has one writer, ``crear``, and everything
else in the codebase can only read. ``tests/test_auditoria.py`` walks the
package with ``ast`` to keep it that way.
"""

from collections.abc import Iterable
from datetime import datetime
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
    valor_anterior: dict[str, Any] | None = None,
    valor_nuevo: dict[str, Any] | None = None,
) -> EventoDominio:
    """The ONLY way a row enters the audit trail."""
    evento = EventoDominio(
        entidad=entidad,
        entidad_id=entidad_id,
        accion=accion,
        autor_id=autor_id,
        datos=datos,
        valor_anterior=valor_anterior,
        valor_nuevo=valor_nuevo,
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
    """The whole timeline of one thing, oldest first."""
    consulta = (
        select(EventoDominio)
        .where(EventoDominio.entidad == entidad, EventoDominio.entidad_id == entidad_id)
        .order_by(EventoDominio.ocurrido_en, EventoDominio.id)
    )
    return list(db.scalars(consulta).all())


def mapa_por_accion(
    db: Session, entidad: str, accion: str, entidad_ids: Iterable[int]
) -> dict[int, dict[str, Any]]:
    """``{entidad_id: datos}`` of the LATEST ``accion`` of each id, in one query.

    RF-034's productivity report needs to know who worked each finished
    service, and for a delivered one that is only knowable from here:
    ``asignacion_servicio`` is deleted the moment the reservation turns
    terminal, and ``reserva.calificacion_habilitada`` captured the operator
    just before that happened (INC-6). Reading it back one reservation at a
    time would be a query per row of the report, so this is the bulk form.
    """
    ids = list(entidad_ids)
    if not ids:
        return {}

    consulta = (
        select(EventoDominio)
        .where(
            EventoDominio.entidad == entidad,
            EventoDominio.accion == accion,
            EventoDominio.entidad_id.in_(ids),
        )
        .order_by(EventoDominio.ocurrido_en, EventoDominio.id)
    )
    # Ascending, so a later row simply overwrites an earlier one and the last
    # value wins - the same "latest" ``obtener_ultimo`` returns.
    return {fila.entidad_id: dict(fila.datos or {}) for fila in db.scalars(consulta).all()}


def listar_por_ids(db: Session, evento_ids: Iterable[int]) -> dict[int, EventoDominio]:
    """``{id: evento}`` for hydrating a page of the audit trail."""
    ids = list(evento_ids)
    if not ids:
        return {}
    consulta = select(EventoDominio).where(EventoDominio.id.in_(ids))
    return {fila.id: fila for fila in db.scalars(consulta).all()}


def listar_entre(
    db: Session, desde: datetime, hasta: datetime, acciones: Iterable[str] | None = None
) -> list[EventoDominio]:
    """Events inside ``[desde, hasta)``, optionally restricted to some actions."""
    consulta = select(EventoDominio).where(
        EventoDominio.ocurrido_en >= desde, EventoDominio.ocurrido_en < hasta
    )
    codigos = set(acciones or ())
    if codigos:
        consulta = consulta.where(EventoDominio.accion.in_(codigos))
    return list(db.scalars(consulta.order_by(EventoDominio.ocurrido_en, EventoDominio.id)).all())
