"""Data access for ``reserva`` and ``reserva_estado_historial``.

The three queries that depend on "is this reservation still active" take the
state set as a PARAMETER. Deriving it belongs to the service layer (it is read
from ``transicion_estado``); repositories stay dumb and take no decisions.
"""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import (
    Pago,
    Reserva,
    ReservaEstadoHistorial,
    Servicio,
    Vehiculo,
)

#: Everything ``ReservaOut`` reads, loaded up front so assembling a page of
#: reservations never degenerates into N+1 queries. The many-to-one sides are
#: already ``lazy="joined"`` on the model; ``cancelada_por`` is not, and the
#: two collections are cheaper with ``selectinload`` than with a join.
OPCIONES_RESERVA = (
    joinedload(Reserva.usuario),
    joinedload(Reserva.vehiculo),
    joinedload(Reserva.bahia),
    joinedload(Reserva.cancelada_por),
    selectinload(Reserva.servicio).selectinload(Servicio.precios),
    selectinload(Reserva.historial).joinedload(ReservaEstadoHistorial.autor),
    selectinload(Reserva.pagos).joinedload(Pago.autor),
)


def _completa(consulta: Select) -> Select:
    return consulta.options(*OPCIONES_RESERVA)


def obtener_por_id(db: Session, reserva_id: int) -> Reserva | None:
    consulta = _completa(select(Reserva).where(Reserva.id == reserva_id))
    return db.scalars(consulta).unique().first()


def obtener_por_codigo(db: Session, codigo: str) -> Reserva | None:
    consulta = _completa(select(Reserva).where(Reserva.codigo == codigo))
    return db.scalars(consulta).unique().first()


def existe_codigo(db: Session, codigo: str) -> bool:
    return db.scalars(select(Reserva.id).where(Reserva.codigo == codigo)).first() is not None


def listar_paginado(
    db: Session,
    *,
    usuario_id: int | None = None,
    estado: str | None = None,
    pagina: int,
    tamanio: int,
) -> tuple[list[Reserva], int]:
    """One page of reservations sorted ``inicio DESC``, plus the total count.

    ``usuario_id`` is the horizontal authorization filter (RF-017 CA-03); the
    service decides whether to pass it, the repository only applies it.
    """
    filtros = []
    if usuario_id is not None:
        filtros.append(Reserva.usuario_id == usuario_id)
    if estado is not None:
        filtros.append(Reserva.estado == estado)

    total = db.scalar(select(func.count(Reserva.id)).where(*filtros)) or 0

    consulta = (
        _completa(select(Reserva).where(*filtros))
        .order_by(Reserva.inicio.desc(), Reserva.id.desc())
        .limit(tamanio)
        .offset((pagina - 1) * tamanio)
    )
    return list(db.scalars(consulta).unique().all()), total


def bahias_ocupadas(
    db: Session, inicio: datetime, fin: datetime, estados_activos: Iterable[str]
) -> set[int]:
    """Bays holding an active reservation that overlaps ``[inicio, fin)``.

    RN-03: two intervals overlap unless one ends before the other starts, i.e.
    ``NOT (nueva.fin <= existente.inicio OR nueva.inicio >= existente.fin)``.
    """
    consulta = select(Reserva.bahia_id).where(
        Reserva.estado.in_(set(estados_activos)),
        Reserva.inicio < fin,
        Reserva.fin > inicio,
    )
    return set(db.scalars(consulta).all())


def listar_ocupacion(
    db: Session, desde: datetime, hasta: datetime, estados_activos: Iterable[str]
) -> list[tuple[int, datetime, datetime]]:
    """``(bahia_id, inicio, fin)`` of every active reservation touching a window.

    The availability algorithm reads the whole day in one query and then tests
    the candidate blocks in memory (RNF-002).
    """
    consulta = select(Reserva.bahia_id, Reserva.inicio, Reserva.fin).where(
        Reserva.estado.in_(set(estados_activos)),
        Reserva.inicio < hasta,
        Reserva.fin > desde,
    )
    return [(fila[0], fila[1], fila[2]) for fila in db.execute(consulta).all()]


def buscar_activas(
    db: Session,
    estados_activos: Iterable[str],
    *,
    codigo: str | None = None,
    placa: str | None = None,
    desde: datetime | None = None,
) -> list[Reserva]:
    """Non terminal reservations matching a reservation code or a plate (RF-019)."""
    consulta = _completa(select(Reserva)).where(Reserva.estado.in_(set(estados_activos)))

    if codigo:
        consulta = consulta.where(Reserva.codigo == codigo)
    if placa:
        consulta = consulta.join(Vehiculo, Vehiculo.id == Reserva.vehiculo_id).where(
            Vehiculo.placa == placa
        )
    if desde is not None:
        # Today's and upcoming ones: a reservation still running counts.
        consulta = consulta.where(Reserva.fin >= desde)

    consulta = consulta.order_by(Reserva.inicio)
    return list(db.scalars(consulta).unique().all())


def crear(
    db: Session,
    *,
    codigo: str,
    usuario_id: int,
    vehiculo_id: int,
    servicio_id: int,
    bahia_id: int,
    inicio: datetime,
    fin: datetime,
    estado: str,
    monto_centimos: int,
    moneda: str,
    modalidad_pago: str,
) -> Reserva:
    reserva = Reserva(
        codigo=codigo,
        usuario_id=usuario_id,
        vehiculo_id=vehiculo_id,
        servicio_id=servicio_id,
        bahia_id=bahia_id,
        inicio=inicio,
        fin=fin,
        estado=estado,
        monto_centimos=monto_centimos,
        moneda=moneda,
        modalidad_pago=modalidad_pago,
    )
    db.add(reserva)
    db.flush()
    return reserva


def agregar_historial(
    db: Session,
    *,
    reserva_id: int,
    estado: str,
    autor_id: int | None,
    ocurrido_en: datetime,
) -> ReservaEstadoHistorial:
    """EXTENSION POINT P7: one row on creation and on every transition."""
    fila = ReservaEstadoHistorial(
        reserva_id=reserva_id,
        estado=estado,
        autor_id=autor_id,
        ocurrido_en=ocurrido_en,
    )
    db.add(fila)
    db.flush()
    return fila


def listar_historial(db: Session, reserva_id: int) -> list[ReservaEstadoHistorial]:
    consulta = (
        select(ReservaEstadoHistorial)
        .where(ReservaEstadoHistorial.reserva_id == reserva_id)
        .order_by(ReservaEstadoHistorial.ocurrido_en, ReservaEstadoHistorial.id)
        .options(joinedload(ReservaEstadoHistorial.autor))
    )
    return list(db.scalars(consulta).unique().all())
