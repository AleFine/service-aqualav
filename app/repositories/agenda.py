"""Data access for ``horario_atencion``, ``dia_no_laborable`` and ``bloqueo_franja``.

Queries only: which rows mean "the shop is closed" is decided in
``app.services.agenda_service``, never here.
"""

from datetime import date, datetime, time

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import BloqueoFranja, DiaNoLaborable, HorarioAtencion


# --------------------------------------------------------------------------
# horario_atencion
# --------------------------------------------------------------------------
def listar_horarios(db: Session) -> list[HorarioAtencion]:
    """Every row, oldest vigency first, so the caller can fold them in order."""
    consulta = select(HorarioAtencion).order_by(
        HorarioAtencion.dia_semana,
        HorarioAtencion.vigente_desde,
        HorarioAtencion.id,
    )
    return list(db.scalars(consulta).all())


def listar_horarios_vigentes(db: Session, fecha: date) -> list[HorarioAtencion]:
    """Rows already in force on ``fecha``, oldest first (the caller keeps the last)."""
    consulta = (
        select(HorarioAtencion)
        .where(HorarioAtencion.vigente_desde <= fecha)
        .order_by(
            HorarioAtencion.dia_semana,
            HorarioAtencion.vigente_desde,
            HorarioAtencion.id,
        )
    )
    return list(db.scalars(consulta).all())


def crear_horario(
    db: Session,
    *,
    dia_semana: int,
    hora_apertura: time | None,
    hora_cierre: time | None,
    vigente_desde: date,
    autor_id: int | None,
) -> HorarioAtencion:
    fila = HorarioAtencion(
        dia_semana=dia_semana,
        hora_apertura=hora_apertura,
        hora_cierre=hora_cierre,
        vigente_desde=vigente_desde,
        autor_id=autor_id,
    )
    db.add(fila)
    db.flush()
    return fila


# --------------------------------------------------------------------------
# dia_no_laborable
# --------------------------------------------------------------------------
def listar_dias_no_laborables(
    db: Session, desde: date | None = None, hasta: date | None = None
) -> list[DiaNoLaborable]:
    consulta = select(DiaNoLaborable)
    if desde is not None:
        consulta = consulta.where(DiaNoLaborable.fecha >= desde)
    if hasta is not None:
        consulta = consulta.where(DiaNoLaborable.fecha <= hasta)
    return list(db.scalars(consulta.order_by(DiaNoLaborable.fecha)).all())


def obtener_dia_no_laborable(db: Session, dia_id: int) -> DiaNoLaborable | None:
    return db.scalars(select(DiaNoLaborable).where(DiaNoLaborable.id == dia_id)).first()


def obtener_dia_no_laborable_por_fecha(db: Session, fecha: date) -> DiaNoLaborable | None:
    return db.scalars(select(DiaNoLaborable).where(DiaNoLaborable.fecha == fecha)).first()


def crear_dia_no_laborable(
    db: Session, *, fecha: date, motivo: str, autor_id: int | None
) -> DiaNoLaborable:
    fila = DiaNoLaborable(fecha=fecha, motivo=motivo, autor_id=autor_id)
    db.add(fila)
    db.flush()
    return fila


def eliminar_dia_no_laborable(db: Session, fila: DiaNoLaborable) -> None:
    db.delete(fila)
    db.flush()


# --------------------------------------------------------------------------
# bloqueo_franja
# --------------------------------------------------------------------------
def listar_bloqueos(
    db: Session,
    desde: datetime,
    hasta: datetime,
    bahia_id: int | None = None,
) -> list[BloqueoFranja]:
    """Blockings overlapping ``[desde, hasta)``.

    A row with ``bahia_id IS NULL`` blocks every bay, so it always comes back.
    """
    consulta = select(BloqueoFranja).where(
        BloqueoFranja.inicio < hasta,
        BloqueoFranja.fin > desde,
    )
    if bahia_id is not None:
        consulta = consulta.where(
            or_(BloqueoFranja.bahia_id == bahia_id, BloqueoFranja.bahia_id.is_(None))
        )
    return list(db.scalars(consulta.order_by(BloqueoFranja.inicio, BloqueoFranja.id)).all())


def obtener_bloqueo(db: Session, bloqueo_id: int) -> BloqueoFranja | None:
    return db.scalars(select(BloqueoFranja).where(BloqueoFranja.id == bloqueo_id)).first()


def crear_bloqueo(
    db: Session,
    *,
    bahia_id: int | None,
    inicio: datetime,
    fin: datetime,
    motivo: str,
    descripcion: str | None,
    autor_id: int | None,
) -> BloqueoFranja:
    fila = BloqueoFranja(
        bahia_id=bahia_id,
        inicio=inicio,
        fin=fin,
        motivo=motivo,
        descripcion=descripcion,
        autor_id=autor_id,
    )
    db.add(fila)
    db.flush()
    return fila


def eliminar_bloqueo(db: Session, fila: BloqueoFranja) -> None:
    db.delete(fila)
    db.flush()
