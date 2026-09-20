"""Data access for ``servicio`` and its price history (``servicio_precio``)."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Servicio, ServicioPrecio


def _con_precios(consulta):
    """Attach the price rows so ``Servicio.precio_vigente`` costs no extra query."""
    return consulta.options(selectinload(Servicio.precios))


def obtener_por_id(db: Session, servicio_id: int) -> Servicio | None:
    consulta = _con_precios(select(Servicio).where(Servicio.id == servicio_id))
    return db.scalars(consulta).first()


def listar(db: Session, *, solo_activos: bool) -> list[Servicio]:
    """Catalog ordered by category then name (RF-009 flow step 3)."""
    consulta = _con_precios(select(Servicio))
    if solo_activos:
        consulta = consulta.where(Servicio.activo.is_(True))
    consulta = consulta.order_by(Servicio.categoria, Servicio.nombre, Servicio.id)
    return list(db.scalars(consulta).all())


def crear(
    db: Session,
    *,
    nombre: str,
    descripcion: str,
    categoria: str,
    duracion_min: int,
) -> Servicio:
    servicio = Servicio(
        nombre=nombre,
        descripcion=descripcion,
        categoria=categoria,
        duracion_min=duracion_min,
        activo=True,
    )
    db.add(servicio)
    db.flush()
    return servicio


def precio_vigente(db: Session, servicio_id: int) -> ServicioPrecio | None:
    """The open price row (``vigente_hasta IS NULL``) of one service."""
    consulta = (
        select(ServicioPrecio)
        .where(
            ServicioPrecio.servicio_id == servicio_id,
            ServicioPrecio.vigente_hasta.is_(None),
        )
        .order_by(ServicioPrecio.vigente_desde.desc(), ServicioPrecio.id.desc())
    )
    return db.scalars(consulta).first()


def cerrar_precio_vigente(
    db: Session, servicio_id: int, momento: datetime
) -> ServicioPrecio | None:
    """Close the open price row. EXTENSION POINT P6: the amount is never updated."""
    actual = precio_vigente(db, servicio_id)
    if actual is not None:
        actual.vigente_hasta = momento
        db.flush()
    return actual


def crear_precio(
    db: Session,
    *,
    servicio_id: int,
    monto_centimos: int,
    moneda: str,
    vigente_desde: datetime,
) -> ServicioPrecio:
    """Insert a new open price row."""
    precio = ServicioPrecio(
        servicio_id=servicio_id,
        monto_centimos=monto_centimos,
        moneda=moneda,
        vigente_desde=vigente_desde,
        vigente_hasta=None,
    )
    db.add(precio)
    db.flush()
    return precio
