"""Data access for ``asignacion_servicio`` and ``cola_espera`` (RF-020)."""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models import AsignacionServicio, ColaEspera, Reserva


def obtener_por_reserva(db: Session, reserva_id: int) -> AsignacionServicio | None:
    consulta = select(AsignacionServicio).where(AsignacionServicio.reserva_id == reserva_id)
    return db.scalars(consulta).first()


def guardar(
    db: Session,
    *,
    reserva_id: int,
    bahia_id: int,
    operario_id: int,
    asignado_por_id: int | None,
    asignado_en: datetime,
    sugerida: bool,
) -> AsignacionServicio:
    """Insert the assignment, or overwrite the one the reservation already had."""
    fila = obtener_por_reserva(db, reserva_id)
    if fila is None:
        fila = AsignacionServicio(reserva_id=reserva_id)
        db.add(fila)

    fila.bahia_id = bahia_id
    fila.operario_id = operario_id
    fila.asignado_por_id = asignado_por_id
    fila.asignado_en = asignado_en
    fila.sugerida = sugerida
    db.flush()
    return fila


def eliminar(db: Session, fila: AsignacionServicio) -> None:
    db.delete(fila)
    db.flush()


def listar_por_reservas(db: Session, reserva_ids: Iterable[int]) -> list[AsignacionServicio]:
    ids = list(reserva_ids)
    if not ids:
        return []
    consulta = (
        select(AsignacionServicio)
        .where(AsignacionServicio.reserva_id.in_(ids))
        .options(joinedload(AsignacionServicio.operario), joinedload(AsignacionServicio.bahia))
    )
    return list(db.scalars(consulta).unique().all())


def carga_por_operario(db: Session, estados_activos: Iterable[str]) -> dict[int, int]:
    """How many still-active services each operator is holding (RF-020 step 2)."""
    consulta = (
        select(AsignacionServicio.operario_id, func.count(AsignacionServicio.id))
        .join(Reserva, Reserva.id == AsignacionServicio.reserva_id)
        .where(Reserva.estado.in_(set(estados_activos)))
        .group_by(AsignacionServicio.operario_id)
    )
    return {fila[0]: fila[1] for fila in db.execute(consulta).all()}


def listar_reservas_de_operario(
    db: Session, operario_id: int, estados_activos: Iterable[str]
) -> list[int]:
    """Reservation ids in the operator's queue, oldest assignment first (CA-02)."""
    consulta = (
        select(AsignacionServicio.reserva_id)
        .join(Reserva, Reserva.id == AsignacionServicio.reserva_id)
        .where(
            AsignacionServicio.operario_id == operario_id,
            Reserva.estado.in_(set(estados_activos)),
        )
        .order_by(AsignacionServicio.asignado_en, AsignacionServicio.id)
    )
    return list(db.scalars(consulta).all())


def listar_activas(db: Session, estados_activos: Iterable[str]) -> list[AsignacionServicio]:
    """Assignments whose reservation is still being worked on."""
    consulta = (
        select(AsignacionServicio)
        .join(Reserva, Reserva.id == AsignacionServicio.reserva_id)
        .where(Reserva.estado.in_(set(estados_activos)))
        .options(joinedload(AsignacionServicio.reserva))
        .order_by(AsignacionServicio.asignado_en, AsignacionServicio.id)
    )
    return list(db.scalars(consulta).unique().all())


def bahias_asignadas(db: Session, estados_activos: Iterable[str]) -> set[int]:
    """Bays physically holding an active service right now (RF-020 CA-01)."""
    consulta = (
        select(AsignacionServicio.bahia_id)
        .join(Reserva, Reserva.id == AsignacionServicio.reserva_id)
        .where(Reserva.estado.in_(set(estados_activos)))
    )
    return set(db.scalars(consulta).all())


# --------------------------------------------------------------------------
# cola_espera (RF-020 flow 2a)
# --------------------------------------------------------------------------
def obtener_cola_por_reserva(db: Session, reserva_id: int) -> ColaEspera | None:
    return db.scalars(select(ColaEspera).where(ColaEspera.reserva_id == reserva_id)).first()


def listar_cola(db: Session) -> list[ColaEspera]:
    consulta = select(ColaEspera).order_by(ColaEspera.posicion, ColaEspera.id)
    return list(db.scalars(consulta).unique().all())


def encolar(db: Session, *, reserva_id: int, posicion: int, tiempo_estimado_min: int) -> ColaEspera:
    fila = obtener_cola_por_reserva(db, reserva_id)
    if fila is None:
        fila = ColaEspera(reserva_id=reserva_id)
        db.add(fila)
    fila.posicion = posicion
    fila.tiempo_estimado_min = tiempo_estimado_min
    db.flush()
    return fila


def desencolar(db: Session, fila: ColaEspera) -> None:
    db.delete(fila)
    db.flush()
