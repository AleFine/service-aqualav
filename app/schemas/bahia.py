"""Bay payloads (RF-020 CA-01 and the bay administration of the v1.0 gap list)."""

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import EstadoBahia


class BahiaResumen(BaseModel):
    """Bay summary nested inside a reservation payload."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str


class BahiaOut(BaseModel):
    """Full projection for the administration screen."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    activa: bool
    # Plain str, like every other state in this API: what "occupied" means is
    # a column value, not a closed union the client has to mirror.
    estado: str


class BahiaCrear(BaseModel):
    nombre: str = Field(min_length=1, max_length=40)
    activa: bool = True


class BahiaActualizar(BaseModel):
    """Any subset of the editable fields."""

    nombre: str | None = Field(default=None, min_length=1, max_length=40)
    activa: bool | None = None
    #: Manual override for the rare case the counter has to free a bay by hand
    #: (a vehicle taken away without a check-out).
    estado: EstadoBahia | None = None
