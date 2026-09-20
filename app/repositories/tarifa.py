"""Data access for the tariff engine (RF-010 delta, RF-011, RF-012).

Queries only. Which factor wins, whether a promotion is still valid and what
the total comes to are decisions, and decisions belong to the service layer.
"""

from datetime import date, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    FactorTipoVehiculo,
    Paquete,
    PaqueteServicio,
    Promocion,
    ReservaAdicional,
    ReservaTarifaDesglose,
    ServicioAdicional,
)

# --------------------------------------------------------------------------
# Vehicle factors (RF-010 delta, RN-04)
# --------------------------------------------------------------------------


def listar_factores_vigentes(
    db: Session, servicio_id: int | None = None, *, incluir_globales: bool = True
) -> list[FactorTipoVehiculo]:
    """Open factor rows (``vigente_hasta IS NULL``), newest vigency last.

    With ``servicio_id`` the result carries that service's own rows plus the
    global ones, so the caller can resolve the precedence in memory instead of
    issuing one query per vehicle type.
    """
    consulta = select(FactorTipoVehiculo).where(FactorTipoVehiculo.vigente_hasta.is_(None))
    if servicio_id is not None:
        if incluir_globales:
            consulta = consulta.where(
                or_(
                    FactorTipoVehiculo.servicio_id == servicio_id,
                    FactorTipoVehiculo.servicio_id.is_(None),
                )
            )
        else:
            consulta = consulta.where(FactorTipoVehiculo.servicio_id == servicio_id)
    consulta = consulta.order_by(
        FactorTipoVehiculo.tipo_vehiculo,
        FactorTipoVehiculo.vigente_desde,
        FactorTipoVehiculo.id,
    )
    return list(db.scalars(consulta).all())


def listar_factores(db: Session, *, solo_vigentes: bool = True) -> list[FactorTipoVehiculo]:
    """Every factor row, for the administration screen (RF-010 delta)."""
    consulta = select(FactorTipoVehiculo)
    if solo_vigentes:
        consulta = consulta.where(FactorTipoVehiculo.vigente_hasta.is_(None))
    consulta = consulta.order_by(
        FactorTipoVehiculo.servicio_id.is_(None).desc(),
        FactorTipoVehiculo.servicio_id,
        FactorTipoVehiculo.tipo_vehiculo,
        FactorTipoVehiculo.id,
    )
    return list(db.scalars(consulta).all())


def factor_vigente(
    db: Session, servicio_id: int | None, tipo_vehiculo: str
) -> FactorTipoVehiculo | None:
    """The open row of one exact ``(servicio_id, tipo_vehiculo)`` pair."""
    consulta = (
        select(FactorTipoVehiculo)
        .where(
            FactorTipoVehiculo.tipo_vehiculo == tipo_vehiculo,
            FactorTipoVehiculo.vigente_hasta.is_(None),
            (
                FactorTipoVehiculo.servicio_id.is_(None)
                if servicio_id is None
                else FactorTipoVehiculo.servicio_id == servicio_id
            ),
        )
        .order_by(FactorTipoVehiculo.vigente_desde.desc(), FactorTipoVehiculo.id.desc())
    )
    return db.scalars(consulta).first()


def cerrar_factor_vigente(
    db: Session, servicio_id: int | None, tipo_vehiculo: str, momento: datetime
) -> FactorTipoVehiculo | None:
    """Close the open row. EXTENSION POINT P6: a factor is never overwritten."""
    actual = factor_vigente(db, servicio_id, tipo_vehiculo)
    if actual is not None:
        actual.vigente_hasta = momento
        db.flush()
    return actual


def crear_factor(
    db: Session,
    *,
    servicio_id: int | None,
    tipo_vehiculo: str,
    factor_milesimas: int,
    vigente_desde: datetime,
) -> FactorTipoVehiculo:
    factor = FactorTipoVehiculo(
        servicio_id=servicio_id,
        tipo_vehiculo=tipo_vehiculo,
        factor_milesimas=factor_milesimas,
        vigente_desde=vigente_desde,
        vigente_hasta=None,
    )
    db.add(factor)
    db.flush()
    return factor


# --------------------------------------------------------------------------
# Packages (RF-011)
# --------------------------------------------------------------------------
def _con_lineas(consulta):
    return consulta.options(selectinload(Paquete.lineas).selectinload(PaqueteServicio.servicio))


def listar_paquetes(db: Session, *, solo_activos: bool) -> list[Paquete]:
    consulta = _con_lineas(select(Paquete))
    if solo_activos:
        consulta = consulta.where(Paquete.activo.is_(True))
    return list(db.scalars(consulta.order_by(Paquete.nombre, Paquete.id)).all())


def obtener_paquete(db: Session, paquete_id: int) -> Paquete | None:
    return db.scalars(_con_lineas(select(Paquete).where(Paquete.id == paquete_id))).first()


def obtener_paquete_por_nombre(db: Session, nombre: str) -> Paquete | None:
    return db.scalars(select(Paquete).where(Paquete.nombre == nombre)).first()


def crear_paquete(
    db: Session,
    *,
    nombre: str,
    descripcion: str,
    precio_centimos: int,
    moneda: str,
    vigente_desde: date,
    vigente_hasta: date | None,
) -> Paquete:
    paquete = Paquete(
        nombre=nombre,
        descripcion=descripcion,
        precio_centimos=precio_centimos,
        moneda=moneda,
        activo=True,
        vigente_desde=vigente_desde,
        vigente_hasta=vigente_hasta,
    )
    db.add(paquete)
    db.flush()
    return paquete


def agregar_linea(db: Session, *, paquete_id: int, servicio_id: int, cantidad: int) -> None:
    db.add(PaqueteServicio(paquete_id=paquete_id, servicio_id=servicio_id, cantidad=cantidad))
    db.flush()


def limpiar_lineas(db: Session, paquete: Paquete) -> None:
    paquete.lineas.clear()
    db.flush()


# --------------------------------------------------------------------------
# Promotions (RF-011)
# --------------------------------------------------------------------------
def listar_promociones(db: Session, *, solo_activas: bool) -> list[Promocion]:
    consulta = select(Promocion)
    if solo_activas:
        consulta = consulta.where(Promocion.activa.is_(True))
    return list(db.scalars(consulta.order_by(Promocion.vigente_desde, Promocion.id)).all())


def listar_promociones_automaticas(db: Session, fecha: date) -> list[Promocion]:
    """Active, coupon-free promotions whose date range covers ``fecha``.

    The weekday filter is NOT applied here: ``dias_semana`` is a comma
    separated string, and parsing it is the service layer's job.
    """
    consulta = select(Promocion).where(
        Promocion.activa.is_(True),
        Promocion.codigo_cupon.is_(None),
        Promocion.vigente_desde <= fecha,
        or_(Promocion.vigente_hasta.is_(None), Promocion.vigente_hasta >= fecha),
    )
    return list(db.scalars(consulta.order_by(Promocion.id)).all())


def obtener_promocion(db: Session, promocion_id: int) -> Promocion | None:
    return db.get(Promocion, promocion_id)


def obtener_promocion_por_nombre(db: Session, nombre: str) -> Promocion | None:
    return db.scalars(select(Promocion).where(Promocion.nombre == nombre)).first()


def obtener_promocion_por_cupon(db: Session, codigo: str) -> Promocion | None:
    """Look the coupon up WITHOUT filtering by validity.

    RF-012 flow 3a has to say WHY a coupon was rejected, and "it expired last
    month" is a different message from "that coupon does not exist".
    """
    return db.scalars(select(Promocion).where(Promocion.codigo_cupon == codigo)).first()


def listar_promociones_solapadas(
    db: Session,
    *,
    servicio_id: int | None,
    paquete_id: int | None,
    vigente_desde: date,
    vigente_hasta: date | None,
    excluir_id: int | None = None,
) -> list[Promocion]:
    """Active automatic promotions on the same target whose ranges intersect.

    Only coupon-free promotions collide (RF-011 flow 2a): two coupons over the
    same service are unambiguous because the customer picks one by typing it.
    An open ended range (``vigente_hasta`` NULL) intersects everything that
    starts on or after its own start.
    """
    consulta = select(Promocion).where(
        Promocion.activa.is_(True),
        Promocion.codigo_cupon.is_(None),
        (
            Promocion.servicio_id.is_(None)
            if servicio_id is None
            else Promocion.servicio_id == servicio_id
        ),
        (
            Promocion.paquete_id.is_(None)
            if paquete_id is None
            else Promocion.paquete_id == paquete_id
        ),
    )
    if excluir_id is not None:
        consulta = consulta.where(Promocion.id != excluir_id)
    if vigente_hasta is not None:
        consulta = consulta.where(Promocion.vigente_desde <= vigente_hasta)
    consulta = consulta.where(
        or_(Promocion.vigente_hasta.is_(None), Promocion.vigente_hasta >= vigente_desde)
    )
    return list(db.scalars(consulta.order_by(Promocion.id)).all())


def crear_promocion(
    db: Session,
    *,
    nombre: str,
    descripcion: str | None,
    tipo_descuento: str,
    valor: int,
    servicio_id: int | None,
    paquete_id: int | None,
    codigo_cupon: str | None,
    dias_semana: str | None,
    vigente_desde: date,
    vigente_hasta: date | None,
) -> Promocion:
    promocion = Promocion(
        nombre=nombre,
        descripcion=descripcion,
        tipo_descuento=tipo_descuento,
        valor=valor,
        servicio_id=servicio_id,
        paquete_id=paquete_id,
        codigo_cupon=codigo_cupon,
        dias_semana=dias_semana,
        vigente_desde=vigente_desde,
        vigente_hasta=vigente_hasta,
        activa=True,
    )
    db.add(promocion)
    db.flush()
    return promocion


# --------------------------------------------------------------------------
# Add-ons (RN-04 "adicionales")
# --------------------------------------------------------------------------
def listar_adicionales(db: Session, *, solo_activos: bool) -> list[ServicioAdicional]:
    consulta = select(ServicioAdicional)
    if solo_activos:
        consulta = consulta.where(ServicioAdicional.activo.is_(True))
    return list(db.scalars(consulta.order_by(ServicioAdicional.nombre, ServicioAdicional.id)).all())


def listar_adicionales_por_ids(db: Session, ids: list[int]) -> list[ServicioAdicional]:
    if not ids:
        return []
    consulta = select(ServicioAdicional).where(ServicioAdicional.id.in_(ids))
    return list(db.scalars(consulta).all())


def obtener_adicional(db: Session, adicional_id: int) -> ServicioAdicional | None:
    return db.get(ServicioAdicional, adicional_id)


def obtener_adicional_por_nombre(db: Session, nombre: str) -> ServicioAdicional | None:
    return db.scalars(select(ServicioAdicional).where(ServicioAdicional.nombre == nombre)).first()


def crear_adicional(
    db: Session,
    *,
    nombre: str,
    descripcion: str | None,
    monto_centimos: int,
    moneda: str,
) -> ServicioAdicional:
    adicional = ServicioAdicional(
        nombre=nombre,
        descripcion=descripcion,
        monto_centimos=monto_centimos,
        moneda=moneda,
        activo=True,
    )
    db.add(adicional)
    db.flush()
    return adicional


# --------------------------------------------------------------------------
# The frozen breakdown (RF-012)
# --------------------------------------------------------------------------
def crear_desglose(db: Session, **campos) -> ReservaTarifaDesglose:
    desglose = ReservaTarifaDesglose(**campos)
    db.add(desglose)
    db.flush()
    return desglose


def crear_reserva_adicional(
    db: Session,
    *,
    reserva_id: int,
    servicio_adicional_id: int | None,
    nombre: str,
    monto_centimos: int,
    moneda: str,
) -> ReservaAdicional:
    fila = ReservaAdicional(
        reserva_id=reserva_id,
        servicio_adicional_id=servicio_adicional_id,
        nombre=nombre,
        monto_centimos=monto_centimos,
        moneda=moneda,
    )
    db.add(fila)
    db.flush()
    return fila
