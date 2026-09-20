"""Availability payloads (RF-013)."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class BloqueDisponible(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    inicio: datetime
    fin: datetime
    bahias_libres: int


class DisponibilidadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fecha: date
    laborable: bool
    servicio_id: int
    duracion_min: int
    bloques: list[BloqueDisponible] = Field(default_factory=list)
    # Filled by scanning forward up to 14 days when no block is free.
    siguiente_fecha_disponible: date | None = None
