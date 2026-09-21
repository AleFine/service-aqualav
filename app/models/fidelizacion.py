"""Loyalty programme: benefits, point movements and redemption coupons (RF-032).

Three tables that between them answer RN-11 - "se acumula 1 punto por cada
S/ 10.00 facturados; 100 puntos = un lavado básico sin costo" - without any of
them owning the arithmetic.

``puntos_movimiento`` is an APPEND-ONLY ledger, like ``evento_dominio``: the
balance is the sum of its ``puntos`` column and nothing else, so there is no
second copy of the balance that could drift away from the movements that
explain it. ``saldo_resultante`` is stored anyway, but as a statement line -
what the customer saw at that moment - never as the source of truth.

``pago_id`` is UNIQUE and that is the whole idempotence of the accrual: one
confirmed payment credits points exactly once, no matter how many times the
receipt is re-issued or the charge replayed with the same key (RF-026 CA-02).
A redemption leaves it NULL, and NULL never collides, so the constraint costs
the redemptions nothing.

``cupon_canje`` is deliberately a POINTER at a ``promocion`` row rather than a
discount of its own. RF-012 already knows how to apply a coupon, freeze it into
the breakdown and report it when it is refused (flow 3a); giving the loyalty
programme a second, parallel discount engine would mean RN-04 was computed in
two places and only one of them was audited. So a redemption MATERIALISES a
promotion with a 100 % discount over the benefit's service and hands the
customer its coupon code; from there on it is an ordinary coupon.
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
from app.models.enums import EstadoCupon

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.pago import Pago
    from app.models.reserva import Reserva
    from app.models.servicio import Servicio
    from app.models.tarifa import Promocion
    from app.models.usuario import Usuario


class Beneficio(Base):
    """What a customer can exchange their points for (RF-032 "beneficio").

    ``stock`` NULL means unlimited; an integer that reaches zero takes the
    benefit out of the listing, which is the first half of flow 4b. The second
    half is ``vigente_hasta``, compared against today on every read exactly
    like a promotion (RF-011 flow 4a) - nothing sweeps, nothing deactivates.
    """

    __tablename__ = "beneficio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    descripcion: Mapped[str | None] = mapped_column(String(400), nullable=True)
    #: RN-11 as DATA: "100 puntos" is this column on the seeded row, not a rule
    #: written into the redemption.
    puntos_requeridos: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Which service the benefit gives away. It is what the materialised
    #: promotion is scoped to, so "un lavado básico sin costo" cannot be spent
    #: on the detailing package.
    servicio_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("servicio.id"), index=True, nullable=True
    )
    #: NULL = unlimited. Decremented on redemption; zero retires it (flow 4b).
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vigente_desde: Mapped[date] = mapped_column(Date, nullable=False)
    #: NULL means open ended.
    vigente_hasta: Mapped[date | None] = mapped_column(Date, nullable=True)
    activo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    servicio: Mapped[Optional["Servicio"]] = relationship("Servicio", lazy="joined")


class PuntosMovimiento(Base):
    """One line of the point ledger (RF-032 "saldo y movimientos de puntos").

    ``puntos`` is SIGNED: positive on an accrual, negative on a redemption, so
    the balance is one ``SUM`` and there is no branch anywhere deciding which
    way a movement counts.
    """

    __tablename__ = "puntos_movimiento"
    __table_args__ = (
        # The idempotence of the accrual, enforced by the database rather than
        # by a check somebody can forget: one confirmed payment, one accrual.
        UniqueConstraint("pago_id", name="uq_puntos_movimiento_pago"),
        Index("ix_puntos_movimiento_usuario_fecha", "usuario_id", "ocurrido_en"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuario.id"), index=True, nullable=False
    )
    #: ``acumulacion`` or ``canje`` (:class:`~app.models.enums.TipoMovimientoPuntos`).
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)
    puntos: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The balance the customer saw right after this line. A statement column,
    #: never the source of truth - that is the sum of ``puntos``.
    saldo_resultante: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Which service earned the points, on an accrual.
    reserva_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("reserva.id", ondelete="SET NULL"), index=True, nullable=True
    )
    #: The confirmed payment that earned them. NULL on a redemption.
    pago_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("pago.id", ondelete="SET NULL"), nullable=True
    )
    #: What was redeemed, on a redemption. NULL on an accrual.
    beneficio_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("beneficio.id"), nullable=True
    )
    #: What was BILLED to produce these points, in cents (RN-11). Kept so a
    #: statement line can be checked against the breakdown that produced it
    #: without joining three tables.
    base_centimos: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    ocurrido_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    usuario: Mapped["Usuario"] = relationship("Usuario", lazy="joined")
    reserva: Mapped[Optional["Reserva"]] = relationship("Reserva")
    pago: Mapped[Optional["Pago"]] = relationship("Pago")
    beneficio: Mapped[Optional["Beneficio"]] = relationship("Beneficio", lazy="joined")


class CuponCanje(Base):
    """The coupon a redemption hands the customer (RF-032 "Salidas").

    It OWNS the entitlement - who may use it, until when, whether it was spent
    - while the ``promocion`` it points at owns the discount. That split is
    what lets RF-012 keep being the only place a tariff is computed: the tariff
    engine sees an ordinary coupon and asks this table one question, "is this
    one yours and still unspent?".
    """

    __tablename__ = "cupon_canje"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuario.id"), index=True, nullable=False
    )
    beneficio_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("beneficio.id"), index=True, nullable=False
    )
    #: The same string the customer types into ``cupon`` when booking, and the
    #: same string stored in ``promocion.codigo_cupon``.
    codigo: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    #: The materialised discount. ``ondelete`` is deliberately absent: a
    #: promotion backing a live coupon must not be deletable behind its back.
    promocion_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("promocion.id"), index=True, nullable=True
    )
    estado: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EstadoCupon.EMITIDO.value,
        server_default=EstadoCupon.EMITIDO.value,
    )
    vence_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The booking that spent it, once one did.
    reserva_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("reserva.id", ondelete="SET NULL"), nullable=True
    )
    usado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    emitido_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    usuario: Mapped["Usuario"] = relationship("Usuario", lazy="joined")
    beneficio: Mapped["Beneficio"] = relationship("Beneficio", lazy="joined")
    promocion: Mapped[Optional["Promocion"]] = relationship("Promocion")
    reserva: Mapped[Optional["Reserva"]] = relationship("Reserva")
