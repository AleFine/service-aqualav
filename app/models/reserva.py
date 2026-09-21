"""Reservations, the data-driven state machine (P3) and the state log (P7)."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import MONEDA_PREDETERMINADA, ModalidadPago

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.asignacion import AsignacionServicio, ColaEspera
    from app.models.bahia import Bahia
    from app.models.notificacion import Recordatorio
    from app.models.pago import Pago
    from app.models.servicio import Servicio
    from app.models.tarifa import ReservaAdicional, ReservaTarifaDesglose
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo


class Reserva(Base):
    __tablename__ = "reserva"
    __table_args__ = (
        # RNF-002 mandates this index for the overlap check (RN-03).
        Index("ix_reserva_bahia_inicio", "bahia_id", "inicio"),
        Index("ix_reserva_usuario_inicio", "usuario_id", "inicio"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(12), unique=True, index=True, nullable=False)
    #: RF-019 v1.0: the token the reception ticket's QR encodes. Opaque and
    #: independent of ``codigo``, which is the one read out loud at the counter.
    codigo_qr: Mapped[str | None] = mapped_column(
        String(24), unique=True, index=True, nullable=True
    )
    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuario.id"), index=True, nullable=False
    )
    vehiculo_id: Mapped[int] = mapped_column(Integer, ForeignKey("vehiculo.id"), nullable=False)
    servicio_id: Mapped[int] = mapped_column(Integer, ForeignKey("servicio.id"), nullable=False)
    bahia_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bahia.id"), index=True, nullable=False
    )
    inicio: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fin: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    estado: Mapped[str] = mapped_column(String(30), nullable=False)
    # Frozen at creation time (RF-014 CA-03): a later price change never moves it.
    monto_centimos: Mapped[int] = mapped_column(Integer, nullable=False)
    moneda: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default=MONEDA_PREDETERMINADA,
        server_default=MONEDA_PREDETERMINADA,
    )
    # EXTENSION POINT: v0.3 adds "en_linea".
    modalidad_pago: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ModalidadPago.PRESENCIAL.value,
        server_default=ModalidadPago.PRESENCIAL.value,
    )
    motivo_cancelacion: Mapped[str | None] = mapped_column(String(300), nullable=True)
    cancelada_por_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("usuario.id"), nullable=True
    )
    cancelada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # RF-019 flow 1a: the customer arrived without booking and the counter
    # opened the service on the spot.
    atencion_sin_reserva: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # Set at check-in (RF-019).
    observaciones_ingreso: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # RF-024 flow 3a: what the customer objected to when the vehicle was
    # handed over, which is what sent the service back to review.
    observacion_revision: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Set at check-out (RF-024).
    conformidad_cliente: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    hora_ingreso: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hora_fin_real: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hora_entrega: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: RF-022 v1.0 step 4: the delivery time as it stands NOW, recalculated on
    #: every state change. It starts as ``fin`` and moves as the service
    #: advances; comparing it against ``fin`` is what raises the delay notice
    #: of flow 4a. Stored rather than derived on every read so the recalculation
    #: happens exactly where something changed, never inside a GET.
    hora_estimada_entrega: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    creada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    usuario: Mapped["Usuario"] = relationship(
        "Usuario",
        back_populates="reservas",
        foreign_keys="Reserva.usuario_id",
        lazy="joined",
    )
    cancelada_por: Mapped[Optional["Usuario"]] = relationship(
        "Usuario", foreign_keys="Reserva.cancelada_por_id"
    )
    vehiculo: Mapped["Vehiculo"] = relationship(
        "Vehiculo", back_populates="reservas", lazy="joined"
    )
    servicio: Mapped["Servicio"] = relationship(
        "Servicio", back_populates="reservas", lazy="joined"
    )
    bahia: Mapped["Bahia"] = relationship("Bahia", back_populates="reservas", lazy="joined")
    historial: Mapped[list["ReservaEstadoHistorial"]] = relationship(
        "ReservaEstadoHistorial",
        back_populates="reserva",
        cascade="all, delete-orphan",
        order_by="ReservaEstadoHistorial.ocurrido_en",
        lazy="selectin",
    )
    pagos: Mapped[list["Pago"]] = relationship(
        "Pago",
        back_populates="reserva",
        cascade="all, delete-orphan",
        order_by="Pago.registrado_en",
        lazy="selectin",
    )
    asignacion: Mapped[Optional["AsignacionServicio"]] = relationship(
        "AsignacionServicio",
        back_populates="reserva",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )
    cola: Mapped[Optional["ColaEspera"]] = relationship(
        "ColaEspera",
        back_populates="reserva",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )
    #: RF-030: the two-hour reminder and the answer it got.
    recordatorio: Mapped[Optional["Recordatorio"]] = relationship(
        "Recordatorio",
        back_populates="reserva",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )
    #: RF-012: why ``monto_centimos`` is what it is. Written once, at creation.
    tarifa: Mapped[Optional["ReservaTarifaDesglose"]] = relationship(
        "ReservaTarifaDesglose",
        back_populates="reserva",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )
    adicionales: Mapped[list["ReservaAdicional"]] = relationship(
        "ReservaAdicional",
        back_populates="reserva",
        cascade="all, delete-orphan",
        order_by="ReservaAdicional.id",
        lazy="selectin",
    )


class TransicionEstado(Base):
    """EXTENSION POINT P3: the allowed moves live in data, never in code.

    RF-021 CA-03: inserting a row here enables a new transition after an API
    restart, with no deploy. Nothing in the codebase may hardcode this table.
    """

    __tablename__ = "transicion_estado"
    __table_args__ = (
        UniqueConstraint("estado_origen", "estado_destino", name="uq_transicion_origen_destino"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    estado_origen: Mapped[str] = mapped_column(String(30), nullable=False)
    estado_destino: Mapped[str] = mapped_column(String(30), nullable=False)
    # FK-free reference to permiso.codigo, on purpose: transitions are data.
    permiso_requerido: Mapped[str] = mapped_column(String(60), nullable=False)
    #: Endpoint that owns this move. When set, the generic POST /estado refuses
    #: it, so the invariants and side effects of that endpoint cannot be skipped.
    endpoint: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: True when reaching this destination means the service is over.
    marca_fin_servicio: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    #: EXTENSION POINT P3, the notification half. RF-029 lists six lifecycle
    #: moments that must reach the customer; WHICH move is each of them is a
    #: property of the move, so it is a column and not a mapping in a service.
    #: NULL means the move is internal and only feeds the in-app feed. A state
    #: inserted as data brings its own notification with it.
    evento_notificacion: Mapped[str | None] = mapped_column(String(40), nullable=True)


class ReservaEstadoHistorial(Base):
    """EXTENSION POINT P7: one row on creation and on every transition."""

    __tablename__ = "reserva_estado_historial"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reserva_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reserva.id", ondelete="CASCADE"), index=True, nullable=False
    )
    estado: Mapped[str] = mapped_column(String(30), nullable=False)
    autor_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("usuario.id"), nullable=True)
    ocurrido_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    reserva: Mapped["Reserva"] = relationship("Reserva", back_populates="historial")
    autor: Mapped[Optional["Usuario"]] = relationship("Usuario", lazy="joined")
