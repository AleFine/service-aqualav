"""Data access for ``vehiculo``."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Vehiculo


def listar_por_usuario(db: Session, usuario_id: int) -> list[Vehiculo]:
    """Every vehicle owned by one customer, newest last."""
    consulta = (
        select(Vehiculo)
        .where(Vehiculo.usuario_id == usuario_id)
        .order_by(Vehiculo.creado_en, Vehiculo.id)
    )
    return list(db.scalars(consulta).all())


def obtener_por_id(db: Session, vehiculo_id: int) -> Vehiculo | None:
    return db.get(Vehiculo, vehiculo_id)


def obtener_por_usuario_y_placa(db: Session, usuario_id: int, placa: str) -> Vehiculo | None:
    """The uniqueness key behind RF-007 CA-02."""
    consulta = select(Vehiculo).where(Vehiculo.usuario_id == usuario_id, Vehiculo.placa == placa)
    return db.scalars(consulta).first()


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
    )
    db.add(vehiculo)
    db.flush()
    return vehiculo
