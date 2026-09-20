"""Wash bays. The MVP seeds four of them (RE-07)."""

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import EstadoBahia

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.asignacion import AsignacionServicio
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
    #: RF-020 CA-01: the assignment marks it occupied and the delivery (or the
    #: cancellation) frees it. Which moves free it is not listed anywhere: a
    #: reservation reaching a TERMINAL state releases its resources, and
    #: terminality is derived from ``transicion_estado`` (P3).
    estado: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EstadoBahia.LIBRE.value,
        server_default=EstadoBahia.LIBRE.value,
    )

    reservas: Mapped[list["Reserva"]] = relationship("Reserva", back_populates="bahia")
    asignaciones: Mapped[list["AsignacionServicio"]] = relationship(
        "AsignacionServicio", back_populates="bahia"
    )
