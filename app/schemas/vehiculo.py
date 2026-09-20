"""Vehicle payloads (RF-007)."""

import re
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import TipoVehiculo

# Both Peruvian formats in circulation, plus the old numeric one.
PLACA_REGEX = re.compile(r"^([A-Z]{3}-\d{3}|[A-Z]\d[A-Z]-\d{3}|\d{4}-[A-Z]{2})$")
MENSAJE_PLACA = "La placa no es válida. Usa el formato ABC-123, A1B-123 o 1234-AB."

ANIO_MINIMO = 1950


def normalizar_placa(valor: str) -> str:
    """Uppercase, drop blanks, and validate against the Peruvian formats."""
    limpia = "".join((valor or "").split()).upper()
    if not PLACA_REGEX.match(limpia):
        raise ValueError(MENSAJE_PLACA)
    return limpia


class VehiculoIn(BaseModel):
    placa: str = Field(max_length=10)
    tipo: TipoVehiculo
    marca: str = Field(min_length=1, max_length=60)
    modelo: str = Field(min_length=1, max_length=60)
    color: str = Field(min_length=1, max_length=40)
    anio: int

    @field_validator("placa")
    @classmethod
    def _placa(cls, valor: str) -> str:
        return normalizar_placa(valor)

    @field_validator("marca", "modelo", "color")
    @classmethod
    def _limpiar(cls, valor: str) -> str:
        return valor.strip()

    @field_validator("anio")
    @classmethod
    def _anio(cls, valor: int) -> int:
        maximo = date.today().year + 1
        if valor < ANIO_MINIMO or valor > maximo:
            raise ValueError(f"El año debe estar entre {ANIO_MINIMO} y {maximo}.")
        return valor


class VehiculoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    placa: str
    tipo: TipoVehiculo
    marca: str
    modelo: str
    color: str
    anio: int
    activo: bool


class VehiculoResumen(BaseModel):
    """Vehicle summary nested inside a reservation payload."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    placa: str
    marca: str
    modelo: str
    tipo: TipoVehiculo
