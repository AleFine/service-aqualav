"""Payment payloads (RF-026)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import EstadoPago, MedioPago
from app.schemas.common import Dinero, nombre_de_autor

MENSAJE_MONTO = "El monto del pago debe ser mayor que cero."


class PagoCrear(BaseModel):
    """``idempotency_key`` also travels in the ``Idempotency-Key`` header."""

    medio: MedioPago
    monto_centimos: int
    motivo_diferencia: str | None = Field(default=None, max_length=300)
    idempotency_key: str | None = Field(default=None, max_length=80)

    @field_validator("monto_centimos")
    @classmethod
    def _monto(cls, valor: int) -> int:
        if valor <= 0:
            raise ValueError(MENSAJE_MONTO)
        return valor

    @field_validator("motivo_diferencia")
    @classmethod
    def _motivo(cls, valor: str | None) -> str | None:
        if valor is None:
            return None
        limpio = valor.strip()
        return limpio or None


class PagoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    monto: Dinero
    medio: MedioPago
    estado: EstadoPago
    registrado_en: datetime
    autor: str | None = None

    @field_validator("autor", mode="before")
    @classmethod
    def _autor(cls, valor: object) -> object:
        return nombre_de_autor(valor)
