"""Customer vehicles (RF-007)."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.reserva import Reserva
    from app.models.usuario import Usuario


class Vehiculo(Base):
    __tablename__ = "vehiculo"
    __table_args__ = (
        # RF-007 CA-02: the same owner cannot register the same plate twice.
        UniqueConstraint("usuario_id", "placa", name="uq_vehiculo_usuario_placa"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuario.id"), index=True, nullable=False
    )
    placa: Mapped[str] = mapped_column(String(10), nullable=False)
    # EXTENSION POINT: sedan | suv | camioneta | motocicleta.
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)
    marca: Mapped[str] = mapped_column(String(60), nullable=False)
    modelo: Mapped[str] = mapped_column(String(60), nullable=False)
    color: Mapped[str] = mapped_column(String(40), nullable=False)
    anio: Mapped[int] = mapped_column(Integer, nullable=False)
    activo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    usuario: Mapped["Usuario"] = relationship("Usuario", back_populates="vehiculos")
    reservas: Mapped[list["Reserva"]] = relationship("Reserva", back_populates="vehiculo")
