"""Service catalog and its price history (RF-009, RF-010, extension point P6)."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import MONEDA_PREDETERMINADA

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.reserva import Reserva


class Servicio(Base):
    __tablename__ = "servicio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(80), nullable=False)
    descripcion: Mapped[str] = mapped_column(String(400), nullable=False)
    # Used for catalog ordering (RF-009 flow step 3).
    categoria: Mapped[str] = mapped_column(
        String(40), nullable=False, default="general", server_default="general"
    )
    # Always a multiple of 15 minutes: the availability grid depends on it.
    duracion_min: Mapped[int] = mapped_column(Integer, nullable=False)
    #: RF-009 v1.0: the reference picture the catalogue shows. A KEY or URL of
    #: the simulated object storage, never the bytes themselves.
    imagen_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    activo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    precios: Mapped[list["ServicioPrecio"]] = relationship(
        "ServicioPrecio",
        back_populates="servicio",
        cascade="all, delete-orphan",
        order_by="ServicioPrecio.vigente_desde",
        lazy="selectin",
    )
    reservas: Mapped[list["Reserva"]] = relationship("Reserva", back_populates="servicio")

    @property
    def precio_vigente(self) -> Optional["ServicioPrecio"]:
        """The open price row (``vigente_hasta is None``), or ``None``."""
        abiertos = [precio for precio in self.precios if precio.vigente_hasta is None]
        if not abiertos:
            return None
        return max(abiertos, key=lambda precio: precio.vigente_desde)


class ServicioPrecio(Base):
    """Price history. A price change closes the current row and inserts a new one."""

    __tablename__ = "servicio_precio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    servicio_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("servicio.id", ondelete="CASCADE"), index=True, nullable=False
    )
    monto_centimos: Mapped[int] = mapped_column(Integer, nullable=False)
    moneda: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default=MONEDA_PREDETERMINADA,
        server_default=MONEDA_PREDETERMINADA,
    )
    vigente_desde: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    # NULL means "currently in effect".
    vigente_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    servicio: Mapped["Servicio"] = relationship("Servicio", back_populates="precios")
