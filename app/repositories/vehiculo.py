"""Data access for ``vehiculo``."""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Reserva, Vehiculo


def listar_por_usuario(
    db: Session, usuario_id: int, *, incluir_inactivos: bool = False
) -> list[Vehiculo]:
    """Vehicles owned by one customer, newest last.

    RF-008 CA-01: a vehicle given up disappears from the ACTIVE list.
    ``incluir_inactivos`` is what the history screen asks for (CA-02), so the
    rows never go away, they only stop being offered.
    """
    consulta = select(Vehiculo).where(Vehiculo.usuario_id == usuario_id)
    if not incluir_inactivos:
        consulta = consulta.where(Vehiculo.activo.is_(True))
    return list(db.scalars(consulta.order_by(Vehiculo.creado_en, Vehiculo.id)).all())


def obtener_por_id(db: Session, vehiculo_id: int) -> Vehiculo | None:
    return db.get(Vehiculo, vehiculo_id)


def obtener_por_usuario_y_placa(
    db: Session, usuario_id: int, placa: str, *, excepto_id: int | None = None
) -> Vehiculo | None:
    """The uniqueness key behind RF-007 CA-02.

    ``excepto_id`` lets an edition keep its own plate: RF-008 would otherwise
    refuse to save a vehicle whose plate did not change.
    """
    consulta = select(Vehiculo).where(Vehiculo.usuario_id == usuario_id, Vehiculo.placa == placa)
    if excepto_id is not None:
        consulta = consulta.where(Vehiculo.id != excepto_id)
    return db.scalars(consulta).first()


def listar_reservas_activas(
    db: Session, vehiculo_id: int, estados_activos: Iterable[str]
) -> list[Reserva]:
    """Live reservations of one vehicle, oldest first (RF-008 flow 3a).

    Which states count as live is a PARAMETER: it is derived from
    ``transicion_estado`` by the service, never listed here (principle P3).
    """
    consulta = (
        select(Reserva)
        .where(Reserva.vehiculo_id == vehiculo_id, Reserva.estado.in_(set(estados_activos)))
        .order_by(Reserva.inicio, Reserva.id)
    )
    return list(db.scalars(consulta).all())


def crear(
    db: Session,
    *,
    usuario_id: int,
    placa: str,
    tipo: str,
    marca: str,
    modelo: str,
    color: str,
    anio: int,
) -> Vehiculo:
    vehiculo = Vehiculo(
        usuario_id=usuario_id,
        placa=placa,
        tipo=tipo,
        marca=marca,
        modelo=modelo,
        color=color,
        anio=anio,
        activo=True,
        verificado=False,
    )
    db.add(vehiculo)
    db.flush()
    return vehiculo
