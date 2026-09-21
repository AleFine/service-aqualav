"""Customer vehicles (RF-007)."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    false,
    func,
    text,
)
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
    #: RF-008: the deletion is LOGICAL. ``False`` takes the vehicle out of the
    #: active list and out of any new booking, and leaves every reservation it
    #: ever had readable (CA-02).
    #: ``text("true")`` y no ``true()``: es literalmente lo que escribió la
    #: migración 0001 (``sa.text("true")``), y la migración no se puede
    #: reescribir. Las columnas booleanas que llegaron en la ``0006`` usan
    #: ``true()``/``false()`` porque ESA migración las escribió así. La
    #: incoherencia es del esquema, no del ORM, y copiarla exactamente es lo
    #: que permite que ``tests/test_migraciones.py`` no necesite excepciones.
    activo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: RN-01 v1.0 asks for a vehicle "registered AND VERIFIED". Verifying is
    #: the counter confirming that the plate on the card is the plate on the
    #: car; nothing else in the SRS describes how it is granted, which is why
    #: ``settings.exigir_vehiculo_verificado`` decides whether booking demands
    #: it (see ``app.services.reserva_service``).
    verificado: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    verificado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    desactivado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    usuario: Mapped["Usuario"] = relationship("Usuario", back_populates="vehiculos")
    reservas: Mapped[list["Reserva"]] = relationship("Reserva", back_populates="vehiculo")
