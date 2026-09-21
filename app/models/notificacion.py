"""Notifications, templates, devices and reminders (RF-029, RF-030).

Four tables and one idea: **what was sent is a row, not a log line**. The MVP
notifier only wrote to the application log, so "se registra el resultado del
envío para trazabilidad" (RF-029) and "se registra el error con su causa"
(CA-02) had nowhere to live. They live here now.

``plantilla_notificacion`` is why no message text is built by concatenating
strings in a service: the body is looked up by ``(evento, canal, idioma)`` and
filled in with the reservation's own data. Adding a language, or changing the
wording of an event, is an UPDATE - never a deploy. Which channels an event
uses is the same table read the other way round: a channel with no template row
for that event is simply not used.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import EstadoEnvio, EstadoRecordatorio

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.reserva import Reserva
    from app.models.usuario import Usuario


class PlantillaNotificacion(Base):
    """The text of one event, on one channel, in one language (RF-029).

    ``cuerpo`` is a ``str.format`` template: ``{codigo}``, ``{estado}``,
    ``{inicio}``, ``{entrega}``... A placeholder the caller did not provide
    renders as an empty string instead of raising, because a missing datum must
    never be the reason a customer is not told their car is ready.
    """

    __tablename__ = "plantilla_notificacion"
    __table_args__ = (
        UniqueConstraint("evento", "canal", "idioma", name="uq_plantilla_evento_canal_idioma"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evento: Mapped[str] = mapped_column(String(40), nullable=False)
    canal: Mapped[str] = mapped_column(String(20), nullable=False)
    idioma: Mapped[str] = mapped_column(String(5), nullable=False)
    asunto: Mapped[str] = mapped_column(String(160), nullable=False)
    cuerpo: Mapped[str] = mapped_column(String(1000), nullable=False)


class Notificacion(Base):
    """One message and how its delivery went (RF-029 "registro del resultado").

    One row per ``(usuario, evento, canal)`` delivery, NOT one per attempt:
    ``intentos`` counts the tries and ``error`` keeps the cause of the last
    one, which is exactly what CA-02 asks to be able to read afterwards.
    """

    __tablename__ = "notificacion"
    __table_args__ = (Index("ix_notificacion_usuario_creada", "usuario_id", "creada_en"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuario.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Null for a notice that is not about a reservation (an account notice).
    reserva_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("reserva.id", ondelete="CASCADE"), index=True, nullable=True
    )
    evento: Mapped[str] = mapped_column(String(40), nullable=False)
    canal: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Where it went: the address for mail, the device token for push, the user
    #: themselves for the in-app feed.
    destino: Mapped[str] = mapped_column(String(200), nullable=False)
    asunto: Mapped[str] = mapped_column(String(160), nullable=False)
    cuerpo: Mapped[str] = mapped_column(String(1000), nullable=False)
    estado_envio: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EstadoEnvio.PENDIENTE.value,
        server_default=EstadoEnvio.PENDIENTE.value,
    )
    intentos: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: The cause of the last failure, in Spanish, never a stack trace (RNF-014).
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    creada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    #: Stamped on the attempt that succeeded. RF-029 CA-01 measures the
    #: distance between the state change and this instant.
    enviado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    usuario: Mapped["Usuario"] = relationship("Usuario", lazy="joined")


class Dispositivo(Base):
    """A device that may receive a push (RF-029 precondition).

    The token is unique across the shop: re-registering one that already exists
    re-activates it and moves it to its new owner, which is what happens when
    two people share a phone or a customer reinstalls the app.
    """

    __tablename__ = "dispositivo"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuario.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_push: Mapped[str] = mapped_column(String(200), unique=True, index=True, nullable=False)
    plataforma: Mapped[str] = mapped_column(String(20), nullable=False)
    activo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    registrado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    usuario: Mapped["Usuario"] = relationship("Usuario", lazy="joined")


#: RF-030: "barrido de reservas que inician EN DOS HORAS". It lives beside the
#: table rather than inside one service because TWO of them need it and they
#: already depend on each other in the other direction: ``recordatorio_service``
#: sends and answers the reminder, and ``reserva_service`` has to re-arm it when
#: RF-015 moves the booking the reminder was pointing at. A leaf module is the
#: one place both can read it from without an import cycle - same reasoning as
#: ``MAXIMO_BAHIAS`` in ``app/models/bahia.py``.
VENTANA_RECORDATORIO = timedelta(hours=2)


class Recordatorio(Base):
    """The two-hour reminder of one reservation and what it was answered (RF-030).

    One row per reservation, which is what makes the sweep idempotent: running
    the scheduler twice in the same window does not send the reminder twice.
    """

    __tablename__ = "recordatorio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reserva_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("reserva.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    programado_para: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    enviado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: ``confirmo`` / ``reprogramo`` / ``cancelo``. NULL is flow 3a: nobody
    #: answered, and the reservation simply stays as it was.
    respuesta: Mapped[str | None] = mapped_column(String(20), nullable=True)
    respondido_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    estado: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EstadoRecordatorio.PENDIENTE.value,
        server_default=EstadoRecordatorio.PENDIENTE.value,
    )

    reserva: Mapped["Reserva"] = relationship(
        "Reserva", back_populates="recordatorio", lazy="joined"
    )
