"""Service evidence and service rating (RF-023, RF-031, RN-10).

Two tables and one mixin, grouped the way ``pago.py`` groups the money: they
are the two halves of "what actually happened to this vehicle" - the shop's
photographic record of it, and the customer's verdict on it.

``evidencia`` carries an UPLOAD STATE, which is the part of RF-023 that is easy
to miss. Flow 4a says a failed upload leaves "evidencia pendiente en el
dispositivo con reintento automatico", so the row has to exist BEFORE the bytes
do: the metadata (which moment, who took it, when) is recorded on the spot and
``estado_carga`` says whether the file made it. ``referencia_cliente`` is the
device's own id for the photograph, and it is what makes the retry of CA-02
idempotent - the same call replayed when the connection comes back completes
the same row instead of creating a seventh photograph.

``calificacion`` is unique per reservation, which is RN-10 read literally: one
rating per service. The window it has to be written inside is NOT stored here;
it is counted from the ``reserva.calificacion_habilitada`` event the check-out
writes, because the moment the window opened cannot be reconstructed afterwards
(the delivery timestamp can be edited, the assignment row is deleted, and a
reservation can reach ``entregado`` from two different states).

``operario_id`` is nullable and is a snapshot, not a live lookup: the
assignment row is deleted when the service becomes terminal
(``bahia_service.liberar_recursos``), so by the time the customer rates, "who
worked on this" only survives in the event log and in this column.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import (
    PUNTUACION_MAXIMA,
    PUNTUACION_MINIMA,
    EstadoCargaEvidencia,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.reserva import Reserva
    from app.models.servicio import Servicio
    from app.models.usuario import Usuario


class Calificable:
    """Running rating aggregate, shared by ``servicio`` and ``usuario``.

    RF-031 asks for "promedio del operario y del servicio ACTUALIZADOS", and
    RF-033 (INC-8) reads those averages as an indicator. What is stored is the
    SUM and the COUNT, never a rounded average: the two integers are exact, the
    average derived from them is exact too, and recomputing them from
    ``calificacion`` after every insert makes the pair impossible to drift.
    """

    @property
    def calificacion_promedio(self) -> float | None:
        """Stars out of five, or ``None`` while nobody has rated yet.

        ``None`` and not ``0.0`` on purpose: a service nobody rated is not a
        service everybody hated, and a screen has to be able to tell them apart.
        """
        conteo = self.calificaciones_count
        if not conteo:
            return None
        return round(self.calificaciones_suma / conteo, 2)


class Evidencia(Base):
    """One photograph of the vehicle, before or after the service (RF-023)."""

    __tablename__ = "evidencia"
    __table_args__ = (
        # The device's own id for the photograph. NULL never collides, so a
        # client that does not send one simply gets a new row every time.
        UniqueConstraint(
            "reserva_id", "referencia_cliente", name="uq_evidencia_reserva_referencia"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reserva_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reserva.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: ``antes`` or ``despues`` (:class:`~app.models.enums.MomentoEvidencia`).
    momento: Mapped[str] = mapped_column(String(10), nullable=False)
    #: Key in the object store. NULL while the bytes have not arrived (flow 4a).
    objeto_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    mime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tamano_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: RF-023 input: "danos preexistentes, objetos de valor".
    observacion: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: RF-023 output: the file is associated to the service "con marca de
    #: tiempo Y AUTOR". Not nullable: an unsigned piece of evidence is not one.
    autor_id: Mapped[int] = mapped_column(Integer, ForeignKey("usuario.id"), nullable=False)
    estado_carga: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EstadoCargaEvidencia.PENDIENTE.value,
        server_default=EstadoCargaEvidencia.PENDIENTE.value,
    )
    intentos: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Why the last attempt failed, so the device can show something better
    #: than "no se pudo" and the shop can see it in the list.
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    referencia_cliente: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: RF-023 output: the "marca de tiempo". It is when the photograph was
    #: TAKEN (registered), not when the bytes finally landed - those are
    #: different instants the moment flow 4a happens, and the customer is
    #: looking at the first one.
    registrada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    subida_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reserva: Mapped["Reserva"] = relationship("Reserva", back_populates="evidencias")
    autor: Mapped["Usuario"] = relationship("Usuario", lazy="joined")


class Calificacion(Base):
    """The customer's verdict on one delivered service (RF-031, RN-10)."""

    __tablename__ = "calificacion"
    __table_args__ = (
        CheckConstraint(
            f"puntuacion BETWEEN {PUNTUACION_MINIMA} AND {PUNTUACION_MAXIMA}",
            name="ck_calificacion_puntuacion",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: RN-10: ONE rating per service. The uniqueness is the rule, not a hint:
    #: flow 3b shows the existing one read-only rather than writing a second.
    reserva_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("reserva.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuario.id"), index=True, nullable=False
    )
    #: Snapshot of who worked the service, taken when the window opened.
    operario_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("usuario.id"), index=True, nullable=True
    )
    #: Denormalised so the average per service is one indexed aggregate and not
    #: a join through ``reserva`` for every read (RF-033 is going to ask a lot).
    servicio_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("servicio.id"), index=True, nullable=False
    )
    puntuacion: Mapped[int] = mapped_column(Integer, nullable=False)
    comentario: Mapped[str | None] = mapped_column(String(500), nullable=True)
    creada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    reserva: Mapped["Reserva"] = relationship("Reserva", back_populates="calificacion")
    usuario: Mapped["Usuario"] = relationship(
        "Usuario", foreign_keys="Calificacion.usuario_id", lazy="joined"
    )
    operario: Mapped[Optional["Usuario"]] = relationship(
        "Usuario", foreign_keys="Calificacion.operario_id", lazy="joined"
    )
    servicio: Mapped["Servicio"] = relationship("Servicio", lazy="joined")
