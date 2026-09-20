"""Bay and operator assignment, and the waiting queue (RF-020)."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.bahia import Bahia
    from app.models.reserva import Reserva
    from app.models.usuario import Usuario


class AsignacionServicio(Base):
    """Who works this service, in which bay, and who decided it (RF-020).

    One row per reservation: a re-assignment overwrites it, and the previous
    value survives in ``evento_dominio`` (P7), which is where RF-036 will read
    the trail from. The row is deleted when the reservation releases its
    resources, so ``cola_espera`` and this table together describe exactly what
    the shop is holding right now.
    """

    __tablename__ = "asignacion_servicio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reserva_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("reserva.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    bahia_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bahia.id"), index=True, nullable=False
    )
    operario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuario.id"), index=True, nullable=False
    )
    asignado_por_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("usuario.id"), nullable=True
    )
    asignado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    #: True when the counter accepted the bay AND the operator the system
    #: suggested, false when it overrode either of them (RF-020 step 2).
    sugerida: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    reserva: Mapped["Reserva"] = relationship("Reserva", back_populates="asignacion")
    bahia: Mapped["Bahia"] = relationship("Bahia", back_populates="asignaciones", lazy="joined")
    operario: Mapped["Usuario"] = relationship(
        "Usuario", foreign_keys="AsignacionServicio.operario_id", lazy="joined"
    )
    asignado_por: Mapped[Optional["Usuario"]] = relationship(
        "Usuario", foreign_keys="AsignacionServicio.asignado_por_id"
    )


class ColaEspera(Base):
    """A received vehicle with no free bay to put it in (RF-020 flow 2a)."""

    __tablename__ = "cola_espera"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reserva_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("reserva.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    posicion: Mapped[int] = mapped_column(Integer, nullable=False)
    tiempo_estimado_min: Mapped[int] = mapped_column(Integer, nullable=False)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    reserva: Mapped["Reserva"] = relationship("Reserva", back_populates="cola", lazy="joined")
