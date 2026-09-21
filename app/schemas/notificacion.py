"""Notification, device, reminder and scheduler payloads (RF-029, RF-030)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import PlataformaDispositivo, RespuestaRecordatorio
from app.schemas.reserva import ReservaOut


class DispositivoIn(BaseModel):
    """Register a device for push (RF-029 precondition)."""

    token_push: str = Field(min_length=8, max_length=200)
    plataforma: PlataformaDispositivo

    @field_validator("token_push")
    @classmethod
    def _token(cls, valor: str) -> str:
        limpio = "".join(valor.split())
        if not limpio:
            raise ValueError("Indica el token del dispositivo.")
        return limpio


class DispositivoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    token_push: str
    plataforma: str
    activo: bool
    registrado_en: datetime


class NotificacionOut(BaseModel):
    """One notice and how its delivery went (RF-029 "registro del resultado")."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    evento: str
    canal: str
    asunto: str
    cuerpo: str
    estado_envio: str
    intentos: int
    # The cause of the last failure (CA-02), in Spanish and free of credentials.
    error: str | None = None
    reserva_id: int | None = None
    creada_en: datetime
    enviado_en: datetime | None = None


class RecordatorioRespuestaIn(BaseModel):
    """Answer to the two-hour reminder: confirm, reschedule or cancel (RF-030)."""

    respuesta: RespuestaRecordatorio
    # Only read when the answer is a cancellation (RF-016 CA-03 wants a reason).
    motivo: str | None = Field(default=None, max_length=300)
    #: RF-015 through RF-030: the block the customer picked when they answered
    #: "reprogramo". Optional, because the reminder is a notification and not a
    #: date picker - an answer without a block stays the recorded intent INC-5
    #: wrote, and one WITH a block performs the real move, RN-06 and the
    #: two-hour window included. Ignored for the other two answers.
    nuevo_inicio: datetime | None = None


class RecordatorioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    reserva_id: int
    programado_para: datetime
    enviado_en: datetime | None = None
    # NULL is flow 3a: nobody answered and the reservation stays as it was.
    respuesta: str | None = None
    respondido_en: datetime | None = None
    estado: str


class RespuestaRecordatorioOut(BaseModel):
    """The reservation after the answer, plus the reminder that was answered."""

    reserva: ReservaOut
    recordatorio: RecordatorioOut


class PlanificadorOut(BaseModel):
    """What one sweep of the scheduler did (RF-030, plan section 4)."""

    momento: datetime
    recordatorios_enviados: int
    reservas_promovidas: list[int] = Field(default_factory=list)
    #: RF-014 flow 2a: the online bookings whose fifteen minutes ran out.
    reservas_expiradas: list[int] = Field(default_factory=list)
