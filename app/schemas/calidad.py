"""Evidence and rating payloads (RF-023, RF-031, RN-10)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import (
    PUNTUACION_MAXIMA,
    PUNTUACION_MINIMA,
    MomentoEvidencia,
)
from app.schemas.common import nombre_de_autor


class EvidenciaIn(BaseModel):
    """Body of ``POST /reservas/{id}/evidencias`` (RF-023).

    ``contenido_base64`` is OPTIONAL, and that is the requirement rather than a
    convenience: flow 4a says a failed upload leaves "evidencia pendiente en el
    dispositivo con reintento automatico", so the device has to be able to
    record that it took a photograph before it manages to deliver it.

    ``referencia_cliente`` is the device's own id for that photograph. Sending
    it makes the retry of CA-02 idempotent - the same body, replayed when the
    connection comes back, completes the row it already opened instead of
    adding a seventh picture to the album.
    """

    momento: MomentoEvidencia
    contenido_base64: str | None = None
    mime: str | None = Field(default=None, max_length=100)
    observacion: str | None = Field(default=None, max_length=500)
    referencia_cliente: str | None = Field(default=None, max_length=80)

    @field_validator("observacion", "referencia_cliente")
    @classmethod
    def _limpiar(cls, valor: str | None) -> str | None:
        limpio = (valor or "").strip()
        return limpio or None


class EvidenciaOut(BaseModel):
    """One photograph as the customer and the shop see it (RF-023 CA-01).

    ``url`` is ``None`` while the bytes have not arrived: the row exists, the
    timestamp and the author are on record, and the picture is not there yet.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    momento: str
    #: RF-023 output: "marca de tiempo Y AUTOR".
    registrada_en: datetime
    autor: str | None = None
    observacion: str | None = None
    estado_carga: str
    url: str | None = None
    mime: str | None = None
    tamano_bytes: int = 0
    intentos: int = 0
    error: str | None = None
    subida_en: datetime | None = None
    referencia_cliente: str | None = None

    @field_validator("autor", mode="before")
    @classmethod
    def _autor(cls, valor: object) -> object:
        return nombre_de_autor(valor)


class CalificacionIn(BaseModel):
    """Body of ``POST /reservas/{id}/calificacion`` (RF-031).

    "Puntuacion 1 a 5 estrellas + comentario OPCIONAL", literally.
    """

    puntuacion: int = Field(ge=PUNTUACION_MINIMA, le=PUNTUACION_MAXIMA)
    comentario: str | None = Field(default=None, max_length=500)

    @field_validator("comentario")
    @classmethod
    def _comentario(cls, valor: str | None) -> str | None:
        limpio = (valor or "").strip()
        return limpio or None


class CalificacionOut(BaseModel):
    """A rating already on record."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    reserva_id: int
    puntuacion: int
    comentario: str | None = None
    creada_en: datetime
    #: Who worked the service, as it stood when the window opened. ``None``
    #: for a service nobody was ever assigned to (a walk-in handled at the
    #: counter, or a reservation delivered before RF-020 existed).
    operario: str | None = None
    operario_id: int | None = None

    @field_validator("operario", mode="before")
    @classmethod
    def _operario(cls, valor: object) -> object:
        return nombre_de_autor(valor)


class CalificacionEstadoOut(BaseModel):
    """Answer of ``GET /reservas/{id}/calificacion`` (RF-031, RN-10).

    It is deliberately a STATE and not just the row: the screen has to be able
    to draw three different things - the empty star picker, the existing rating
    in read-only mode (flow 3b) and the "el plazo vencio" notice (flow 3a) -
    and asking it to infer which one from an error code would be worse.
    """

    calificacion: CalificacionOut | None = None
    #: True only when the window is open AND nothing has been written yet.
    puede_calificar: bool = False
    #: When the delivery opened the window, read from the domain event.
    habilitada_en: datetime | None = None
    #: ``habilitada_en`` plus the seven calendar days of RN-10.
    vence_en: datetime | None = None
    #: Why ``puede_calificar`` is false, in Spanish, ready to render.
    motivo: str | None = None
