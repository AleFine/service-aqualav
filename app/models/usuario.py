"""Application user (replaces the template's ``users`` table)."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import EstadoCuenta

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.bahia import Bahia
    from app.models.reserva import Reserva
    from app.models.rol import Rol
    from app.models.vehiculo import Vehiculo


class Usuario(Base):
    __tablename__ = "usuario"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombres: Mapped[str] = mapped_column(String(80), nullable=False)
    apellidos: Mapped[str] = mapped_column(String(80), nullable=False)
    correo: Mapped[str] = mapped_column(String(160), unique=True, index=True, nullable=False)
    telefono: Mapped[str] = mapped_column(String(20), nullable=False)
    hash_password: Mapped[str] = mapped_column(String(255), nullable=False)
    rol_id: Mapped[int] = mapped_column(Integer, ForeignKey("rol.id"), nullable=False)
    #: RF-035: the bay an operator usually works in. It only PRE-SELECTS the
    #: suggestion of RF-020; it never reserves the bay for them.
    bahia_habitual_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bahia.id"), nullable=True
    )
    # EXTENSION POINT: v0.2 adds "pendiente_verificacion".
    estado_cuenta: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=EstadoCuenta.ACTIVA.value,
        server_default=EstadoCuenta.ACTIVA.value,
    )
    intentos_fallidos: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    bloqueado_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    rol: Mapped["Rol"] = relationship("Rol", back_populates="usuarios", lazy="joined")
    bahia_habitual: Mapped[Optional["Bahia"]] = relationship("Bahia", lazy="joined")
    vehiculos: Mapped[list["Vehiculo"]] = relationship(
        "Vehiculo", back_populates="usuario", cascade="all, delete-orphan"
    )
    reservas: Mapped[list["Reserva"]] = relationship(
        "Reserva", back_populates="usuario", foreign_keys="Reserva.usuario_id"
    )

    @property
    def nombre_completo(self) -> str:
        """``"Ana Torres"`` - used as the ``autor`` label in payloads."""
        return f"{self.nombres} {self.apellidos}".strip()
