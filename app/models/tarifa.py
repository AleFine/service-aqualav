"""Tariff engine: factors, packages, promotions, add-ons and the breakdown.

RF-010 (delta), RF-011, RF-012 and RN-04. Everything here exists to make one
sentence computable and auditable::

    tarifa final = (precio base x factor por tipo de vehiculo)
                   + adicionales - descuentos

EXTENSION POINT P6 all the way through: every amount is an INTEGER number of
cents with an explicit currency (RN-12), and the vehicle factor is stored in
THOUSANDTHS (``1300`` means 1.3) so the arithmetic never needs a float. A
``Numeric`` factor would have forced either a float multiplication - which is
exactly how 3000 x 1.3 stops being 3900 - or a decimal context the rest of the
codebase does not have.

``factor_tipo_vehiculo``, like ``servicio_precio``, is VERSIONED instead of
updated: a change closes the open row and inserts a new one, so a reservation
priced last month stays explainable (RF-010 delta, RNF-014).
"""

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import FACTOR_BASE_MILESIMAS, MONEDA_PREDETERMINADA

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.reserva import Reserva
    from app.models.servicio import Servicio


class FactorTipoVehiculo(Base):
    """How much more (or less) a vehicle type costs for a service (RN-04).

    ``servicio_id`` NULL is the GLOBAL factor of that vehicle type: the shop
    charges an SUV 1.3x everywhere unless a particular service overrides it.
    Resolution order is therefore "the service's own row first, the global one
    second, and 1.0 when neither exists", which is what keeps the catalogue
    correct the moment a service is created and before anyone configures it.
    """

    __tablename__ = "factor_tipo_vehiculo"
    __table_args__ = (
        Index("ix_factor_servicio_tipo", "servicio_id", "tipo_vehiculo", "vigente_desde"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: NULL = applies to every service that has no row of its own.
    servicio_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("servicio.id", ondelete="CASCADE"), index=True, nullable=True
    )
    #: One of ``TipoVehiculo``; a plain string so a new type is a data change.
    tipo_vehiculo: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Thousandths: 1000 = 1.0, 1300 = 1.3. Never a float (P6).
    #: ``server_default`` as well as ``default`` because migration ``0005``
    #: created the column with one: the ORM and the migration have to describe
    #: the same table, or a row inserted by hand outside the ORM gets a
    #: different factor than a row inserted through it.
    factor_milesimas: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=FACTOR_BASE_MILESIMAS,
        server_default=str(FACTOR_BASE_MILESIMAS),
    )
    vigente_desde: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    #: NULL means "currently in effect", exactly like ``servicio_precio``.
    vigente_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    servicio: Mapped[Optional["Servicio"]] = relationship("Servicio")


class Paquete(Base):
    """Several services sold together at a preferential price (RF-011)."""

    __tablename__ = "paquete"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    descripcion: Mapped[str] = mapped_column(String(400), nullable=False)
    precio_centimos: Mapped[int] = mapped_column(Integer, nullable=False)
    moneda: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default=MONEDA_PREDETERMINADA,
        server_default=MONEDA_PREDETERMINADA,
    )
    activo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    vigente_desde: Mapped[date] = mapped_column(Date, nullable=False)
    #: NULL means open ended.
    vigente_hasta: Mapped[date | None] = mapped_column(Date, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    lineas: Mapped[list["PaqueteServicio"]] = relationship(
        "PaqueteServicio",
        back_populates="paquete",
        cascade="all, delete-orphan",
        order_by="PaqueteServicio.id",
        lazy="selectin",
    )


class PaqueteServicio(Base):
    """One line of a package: which service, how many times."""

    __tablename__ = "paquete_servicio"
    __table_args__ = (UniqueConstraint("paquete_id", "servicio_id", name="uq_paquete_servicio"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paquete_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("paquete.id", ondelete="CASCADE"), index=True, nullable=False
    )
    servicio_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("servicio.id"), index=True, nullable=False
    )
    cantidad: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    paquete: Mapped["Paquete"] = relationship("Paquete", back_populates="lineas")
    servicio: Mapped["Servicio"] = relationship("Servicio", lazy="joined")


class Promocion(Base):
    """A discount with a validity window (RF-011).

    Three independent switches decide whether it applies: the date range, the
    weekdays (``dias_semana``, a comma separated list of ``weekday()`` numbers;
    NULL = every day) and the coupon (``codigo_cupon``). A promotion WITHOUT a
    coupon applies automatically; one WITH a coupon only when the customer
    types it, which is why two coupon promotions over the same service never
    collide and two automatic ones do (flow 2a).

    Nothing expires it: ``vigente_hasta`` is compared against the service date
    on every calculation, so an expired promotion stops applying by itself and
    the regular price comes back (flow 4a).
    """

    __tablename__ = "promocion"
    __table_args__ = (Index("ix_promocion_servicio_vigencia", "servicio_id", "vigente_desde"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    descripcion: Mapped[str | None] = mapped_column(String(400), nullable=True)
    #: One of ``TipoDescuento``: ``porcentaje`` or ``monto``.
    tipo_descuento: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Percentage points (10 = 10 %) or integer cents, per ``tipo_descuento``.
    valor: Mapped[int] = mapped_column(Integer, nullable=False)
    #: NULL = applies to every service.
    servicio_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("servicio.id", ondelete="CASCADE"), index=True, nullable=True
    )
    #: Set when the promotion advertises a package instead of a single service.
    paquete_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("paquete.id", ondelete="CASCADE"), index=True, nullable=True
    )
    #: NULL = no coupon, so it applies on its own.
    codigo_cupon: Mapped[str | None] = mapped_column(
        String(30), unique=True, index=True, nullable=True
    )
    #: ``"0,1,2"`` style list of ``datetime.weekday()`` values; NULL = all days.
    dias_semana: Mapped[str | None] = mapped_column(String(20), nullable=True)
    vigente_desde: Mapped[date] = mapped_column(Date, nullable=False)
    #: NULL means open ended.
    vigente_hasta: Mapped[date | None] = mapped_column(Date, nullable=True)
    activa: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    creada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    servicio: Mapped[Optional["Servicio"]] = relationship("Servicio")
    paquete: Mapped[Optional["Paquete"]] = relationship("Paquete")


class ServicioAdicional(Base):
    """An extra the customer can add to a service (RN-04 "adicionales")."""

    __tablename__ = "servicio_adicional"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    descripcion: Mapped[str | None] = mapped_column(String(400), nullable=True)
    monto_centimos: Mapped[int] = mapped_column(Integer, nullable=False)
    moneda: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default=MONEDA_PREDETERMINADA,
        server_default=MONEDA_PREDETERMINADA,
    )
    activo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class ReservaAdicional(Base):
    """An add-on as it was bought: name and amount FROZEN at creation time.

    The name is copied rather than joined on purpose. Renaming or repricing an
    add-on afterwards must not rewrite what a customer already paid for, which
    is the same reason ``reserva.monto_centimos`` exists (RF-014 CA-03).
    """

    __tablename__ = "reserva_adicional"
    __table_args__ = (
        UniqueConstraint("reserva_id", "servicio_adicional_id", name="uq_reserva_adicional"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reserva_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reserva.id", ondelete="CASCADE"), index=True, nullable=False
    )
    servicio_adicional_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("servicio_adicional.id"), nullable=True
    )
    nombre: Mapped[str] = mapped_column(String(80), nullable=False)
    monto_centimos: Mapped[int] = mapped_column(Integer, nullable=False)
    moneda: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default=MONEDA_PREDETERMINADA,
        server_default=MONEDA_PREDETERMINADA,
    )

    reserva: Mapped["Reserva"] = relationship("Reserva", back_populates="adicionales")


class ReservaTarifaDesglose(Base):
    """The audit trail RF-012 asks for: one row per reservation, never updated.

    ``reserva.monto_centimos`` is the number the shop charges; this row is WHY
    it is that number. Every term of RN-04 is stored separately, including the
    rejected coupon and its reason (flow 3a) and the incident raised when the
    discount exceeded the total (flow 4a), so the calculation can be re-read
    long after the prices, the factors and the promotion have moved on.
    """

    __tablename__ = "reserva_tarifa_desglose"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reserva_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("reserva.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    precio_base_centimos: Mapped[int] = mapped_column(Integer, nullable=False)
    tipo_vehiculo: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Same alignment with migration ``0005`` as ``FactorTipoVehiculo``.
    factor_milesimas: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=FACTOR_BASE_MILESIMAS,
        server_default=str(FACTOR_BASE_MILESIMAS),
    )
    base_ajustada_centimos: Mapped[int] = mapped_column(Integer, nullable=False)
    adicionales_centimos: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    descuento_centimos: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    promocion_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("promocion.id"), nullable=True
    )
    promocion_nombre: Mapped[str | None] = mapped_column(String(80), nullable=True)
    cupon_aplicado: Mapped[str | None] = mapped_column(String(30), nullable=True)
    #: RF-012 flow 3a: what the customer typed and why it was not honoured.
    cupon_rechazado: Mapped[str | None] = mapped_column(String(30), nullable=True)
    motivo_rechazo_cupon: Mapped[str | None] = mapped_column(String(300), nullable=True)
    total_centimos: Mapped[int] = mapped_column(Integer, nullable=False)
    moneda: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default=MONEDA_PREDETERMINADA,
        server_default=MONEDA_PREDETERMINADA,
    )
    #: RF-012 flow 4a: set when the total had to be clamped to zero.
    incidencia: Mapped[str | None] = mapped_column(String(300), nullable=True)
    calculado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    reserva: Mapped["Reserva"] = relationship("Reserva", back_populates="tarifa")
    promocion: Mapped[Optional["Promocion"]] = relationship("Promocion")
