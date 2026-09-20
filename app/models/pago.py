"""Payments registered at the counter (RF-026)."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import MONEDA_PREDETERMINADA, EstadoPago

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.reserva import Reserva
    from app.models.usuario import Usuario


class Pago(Base):
    __tablename__ = "pago"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reserva_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reserva.id", ondelete="CASCADE"), index=True, nullable=False
    )
    monto_centimos: Mapped[int] = mapped_column(Integer, nullable=False)
    moneda: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default=MONEDA_PREDETERMINADA,
        server_default=MONEDA_PREDETERMINADA,
    )
    # efectivo | tarjeta_pos | transferencia
    medio: Mapped[str] = mapped_column(String(20), nullable=False)
    estado: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EstadoPago.CONFIRMADO.value,
        server_default=EstadoPago.CONFIRMADO.value,
    )
    # EXTENSION POINT P6: replaying the same key returns the existing payment.
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    # EXTENSION POINT: payment gateway reference (v0.3).
    referencia_externa: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Required when monto_centimos differs from reserva.monto_centimos.
    motivo_diferencia: Mapped[str | None] = mapped_column(String(300), nullable=True)
    autor_id: Mapped[int] = mapped_column(Integer, ForeignKey("usuario.id"), nullable=False)
    registrado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    reserva: Mapped["Reserva"] = relationship("Reserva", back_populates="pagos")
    autor: Mapped["Usuario"] = relationship("Usuario", lazy="joined")
