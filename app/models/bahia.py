"""Wash bays. The MVP seeds four of them (RE-07)."""

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.reserva import Reserva

#: Hard limit imposed by the physical shop (RE-07).
MAXIMO_BAHIAS = 4


class Bahia(Base):
    __tablename__ = "bahia"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    activa: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    reservas: Mapped[list["Reserva"]] = relationship("Reserva", back_populates="bahia")
