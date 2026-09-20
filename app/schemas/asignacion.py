"""Assignment and waiting queue payloads (RF-020)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.bahia import BahiaResumen
from app.schemas.common import nombre_de_autor
from app.schemas.reserva import ReservaOut


class AsignacionIn(BaseModel):
    """Body of ``POST /reservas/{id}/asignacion``.

    Every field is optional: sending an empty body accepts the bay and the
    operator the system suggests, which is the normal path of RF-020 step 2.
    """

    bahia_id: int | None = Field(default=None, ge=1)
    operario_id: int | None = Field(default=None, ge=1)
    #: Re-sent as ``true`` after an ``OPERARIO_OCUPADO`` reply (flow 3a).
    confirmar_operario_ocupado: bool = False


class AsignacionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    bahia: BahiaResumen
    operario: str
    operario_id: int
    asignado_por: str | None = None
    asignado_en: datetime
    #: True when the counter kept both suggestions untouched.
    sugerida: bool

    @field_validator("asignado_por", "operario", mode="before")
    @classmethod
    def _nombre(cls, valor: object) -> object:
        return nombre_de_autor(valor)


class ColaEsperaOut(BaseModel):
    """RF-020 flow 2a: no bay was free, the vehicle waits with an estimate."""

    model_config = ConfigDict(from_attributes=True)

    posicion: int
    tiempo_estimado_min: int


class SugerenciaOut(BaseModel):
    """What the system would have picked, so the screen can pre-select it."""

    bahia: BahiaResumen | None = None
    operario_id: int | None = None
    operario: str | None = None


class ResultadoAsignacionOut(BaseModel):
    """Answer of ``POST /reservas/{id}/asignacion``.

    Exactly one of ``asignacion`` and ``cola`` is filled. The queue is not an
    error: the counter DID receive the vehicle, the shop simply has no bay for
    it yet, and the reservation stays where it was so the same call can be
    retried when one frees up (flow 2a).
    """

    reserva: ReservaOut
    asignacion: AsignacionOut | None = None
    cola: ColaEsperaOut | None = None
    sugerencia: SugerenciaOut | None = None
