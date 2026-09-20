"""Service catalog payloads (RF-009, RF-010)."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import MONEDA_PREDETERMINADA
from app.schemas.common import Dinero

PASO_DURACION_MIN = 15
MENSAJE_DURACION = "La duración debe ser un múltiplo de 15 minutos y mayor que cero."
MENSAJE_MONTO = "El monto debe ser mayor que cero."


def validar_duracion(valor: int) -> int:
    if valor <= 0 or valor % PASO_DURACION_MIN != 0:
        raise ValueError(MENSAJE_DURACION)
    return valor


def validar_monto(valor: int) -> int:
    if valor <= 0:
        raise ValueError(MENSAJE_MONTO)
    return valor


class ServicioOut(BaseModel):
    """``precio`` is an OBJECT, never a bare number (RF-009 'Cómo escala')."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    descripcion: str
    categoria: str
    duracion_min: int
    activo: bool
    precio: Dinero


class ServicioResumen(BaseModel):
    """Service summary nested inside a reservation payload."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    duracion_min: int


class ServicioCrear(BaseModel):
    nombre: str = Field(min_length=1, max_length=80)
    descripcion: str = Field(min_length=1, max_length=400)
    categoria: str = Field(default="general", max_length=40)
    duracion_min: int
    monto_centimos: int
    moneda: str = Field(default=MONEDA_PREDETERMINADA, min_length=3, max_length=3)

    @field_validator("duracion_min")
    @classmethod
    def _duracion(cls, valor: int) -> int:
        return validar_duracion(valor)

    @field_validator("monto_centimos")
    @classmethod
    def _monto(cls, valor: int) -> int:
        return validar_monto(valor)


class ServicioActualizar(BaseModel):
    """Any subset of the creation fields, plus ``activo``."""

    nombre: str | None = Field(default=None, min_length=1, max_length=80)
    descripcion: str | None = Field(default=None, min_length=1, max_length=400)
    categoria: str | None = Field(default=None, max_length=40)
    duracion_min: int | None = None
    monto_centimos: int | None = None
    moneda: str | None = Field(default=None, min_length=3, max_length=3)
    activo: bool | None = None

    @field_validator("duracion_min")
    @classmethod
    def _duracion(cls, valor: int | None) -> int | None:
        return None if valor is None else validar_duracion(valor)

    @field_validator("monto_centimos")
    @classmethod
    def _monto(cls, valor: int | None) -> int | None:
        return None if valor is None else validar_monto(valor)
