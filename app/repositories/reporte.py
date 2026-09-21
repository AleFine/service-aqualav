"""Data access for the panel, the reports and their exports (RF-033, RF-034).

Two kinds of query live here and they are worth telling apart.

The first two are the RAW MATERIAL of every indicator: the services that were
actually performed inside a period, and the payments that were taken inside
it. Everything RF-033 lists - attended services, revenue, bay occupancy,
average ticket, average attention time and average rating - is derived from
those two lists in ``reporte_service`` and nowhere else. That is not tidiness:
RF-033 CA-01 says the panel's totals must match the detailed report's, and the
only way to guarantee that is to have ONE calculation rather than two that
happen to agree today.

"Was performed" is ``hora_fin_real IS NOT NULL``, never a state name. That
column is stamped by whichever transition carries ``marca_fin_servicio`` in
``transicion_estado`` (P3), so a shop that inserts a twelfth state and moves
the flag onto it gets correct reports without touching a line of this file.

The rest is the bookkeeping of ``reporte_exportacion`` (RF-034 flow 4a).
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import Pago, ReporteExportacion, Reserva, Servicio


# --------------------------------------------------------------------------
# Raw material of every indicator (RF-033, RF-034)
# --------------------------------------------------------------------------
def listar_atendidas(db: Session, desde: datetime, hasta: datetime) -> list[Reserva]:
    """Services FINISHED inside ``[desde, hasta)``, oldest first.

    The anchor is ``hora_fin_real`` and not ``inicio``: a report of a month is
    about the work that month produced, and a booking that started on the 31st
    and finished on the 1st belongs to the month it was worked in. It is also
    the only anchor that is stamped by the state machine itself, so it cannot
    disagree with what the shop actually did.
    """
    consulta = (
        select(Reserva)
        .options(
            joinedload(Reserva.usuario),
            joinedload(Reserva.vehiculo),
            joinedload(Reserva.bahia),
            selectinload(Reserva.servicio).selectinload(Servicio.precios),
            selectinload(Reserva.calificacion),
        )
        .where(
            Reserva.hora_fin_real.is_not(None),
            Reserva.hora_fin_real >= desde,
            Reserva.hora_fin_real < hasta,
        )
        .order_by(Reserva.hora_fin_real, Reserva.id)
    )
    return list(db.scalars(consulta).unique().all())


def listar_pagos(
    db: Session, desde: datetime, hasta: datetime, estados: Iterable[str]
) -> list[Pago]:
    """Payments registered inside ``[desde, hasta)`` whose state is in ``estados``.

    ``estados`` is a parameter like every other state set in this package: the
    repository takes no decision about which payments count as revenue. The
    service passes :data:`~app.models.enums.ESTADOS_PAGO_INGRESADO`.
    """
    codigos = set(estados)
    if not codigos:
        return []
    consulta = (
        select(Pago)
        .options(
            joinedload(Pago.reserva).joinedload(Reserva.usuario),
            joinedload(Pago.reserva).joinedload(Reserva.servicio),
        )
        .where(
            Pago.estado.in_(codigos),
            Pago.registrado_en >= desde,
            Pago.registrado_en < hasta,
        )
        .order_by(Pago.registrado_en, Pago.id)
    )
    return list(db.scalars(consulta).unique().all())


# --------------------------------------------------------------------------
# Export bookkeeping (RF-034 flow 4a)
# --------------------------------------------------------------------------
def crear_exportacion(
    db: Session,
    *,
    tipo: str,
    formato: str,
    filtros: dict[str, Any],
    estado: str,
    solicitado_por_id: int,
    solicitado_en: datetime,
) -> ReporteExportacion:
    fila = ReporteExportacion(
        tipo=tipo,
        formato=formato,
        filtros=filtros,
        estado=estado,
        solicitado_por_id=solicitado_por_id,
        solicitado_en=solicitado_en,
    )
    db.add(fila)
    db.flush()
    return fila


def obtener_exportacion(db: Session, exportacion_id: int) -> ReporteExportacion | None:
    return db.get(ReporteExportacion, exportacion_id)


def listar_exportaciones(
    db: Session, *, solicitado_por_id: int | None = None, limite: int = 50
) -> list[ReporteExportacion]:
    """The export requests, newest first."""
    consulta = select(ReporteExportacion).order_by(
        ReporteExportacion.solicitado_en.desc(), ReporteExportacion.id.desc()
    )
    if solicitado_por_id is not None:
        consulta = consulta.where(ReporteExportacion.solicitado_por_id == solicitado_por_id)
    return list(db.scalars(consulta.limit(limite)).all())


def listar_pendientes(db: Session, estado: str, *, limite: int = 20) -> list[ReporteExportacion]:
    """Exports waiting for the scheduler, oldest first (fair queueing)."""
    consulta = (
        select(ReporteExportacion)
        .where(ReporteExportacion.estado == estado)
        .order_by(ReporteExportacion.solicitado_en, ReporteExportacion.id)
        .limit(limite)
    )
    return list(db.scalars(consulta).all())


def cerrar_exportacion(
    db: Session,
    fila: ReporteExportacion,
    *,
    estado: str,
    momento: datetime,
    archivo_key: str | None = None,
    filas: int = 0,
    error: str | None = None,
) -> ReporteExportacion:
    """Write down how the generation ended.

    This is an UPDATE, and it is the only one in the reporting package. It
    touches ``reporte_exportacion``, never ``evento_dominio``: the audit trail
    stays insert only (RNF-014), and the LIFECYCLE of an export request is
    ordinary state, not an audit record.
    """
    fila.estado = estado
    fila.generado_en = momento
    fila.archivo_key = archivo_key
    fila.filas = filas
    fila.error = error
    db.flush()
    return fila
