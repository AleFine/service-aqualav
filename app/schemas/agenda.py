"""Agenda payloads: opening hours, holidays, blockings and the board (RF-018)."""

from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import MotivoBloqueo
from app.schemas.bahia import BahiaResumen
from app.schemas.common import nombre_de_autor

#: The two views RF-018 asks for.
VISTA_DIA = "dia"
VISTA_SEMANA = "semana"
VISTAS = (VISTA_DIA, VISTA_SEMANA)


class HorarioAtencionOut(BaseModel):
    """The opening window of one weekday, as data (RN-07)."""

    model_config = ConfigDict(from_attributes=True)

    dia_semana: int
    hora_apertura: time | None = None
    hora_cierre: time | None = None
    vigente_desde: date
    cerrado: bool = False


class HorarioAtencionIn(BaseModel):
    """Body of ``PUT /agenda/horarios/{dia_semana}``.

    Leaving both hours out closes the shop that weekday from today on.
    """

    hora_apertura: time | None = None
    hora_cierre: time | None = None
    vigente_desde: date | None = None

    @model_validator(mode="after")
    def _ventana(self) -> "HorarioAtencionIn":
        # A field validator would not fire when the field is simply absent,
        # which is exactly the case that has to be caught here.
        if (self.hora_apertura is None) != (self.hora_cierre is None):
            raise ValueError(
                "Indica la hora de apertura y la de cierre juntas, "
                "o ninguna de las dos para cerrar ese día."
            )
        if (
            self.hora_apertura is not None
            and self.hora_cierre is not None
            and self.hora_cierre <= self.hora_apertura
        ):
            raise ValueError("La hora de cierre debe ser posterior a la de apertura.")
        return self


class DiaNoLaborableOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    fecha: date
    motivo: str
    autor: str | None = None

    @field_validator("autor", mode="before")
    @classmethod
    def _autor(cls, valor: object) -> object:
        return nombre_de_autor(valor)


class DiaNoLaborableIn(BaseModel):
    fecha: date
    motivo: str = Field(min_length=1, max_length=200)

    @field_validator("motivo")
    @classmethod
    def _motivo(cls, valor: str) -> str:
        limpio = valor.strip()
        if not limpio:
            raise ValueError("Indica el motivo del día no laborable.")
        return limpio


class BloqueoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    #: ``None`` blocks the whole shop, every bay at once.
    bahia: BahiaResumen | None = None
    inicio: datetime
    fin: datetime
    motivo: str
    descripcion: str | None = None
    autor: str | None = None

    @field_validator("autor", mode="before")
    @classmethod
    def _autor(cls, valor: object) -> object:
        return nombre_de_autor(valor)


class BloqueoIn(BaseModel):
    """Body of ``POST /agenda/bloqueos``. ``bahia_id`` absent blocks every bay."""

    bahia_id: int | None = Field(default=None, ge=1)
    inicio: datetime
    fin: datetime
    motivo: MotivoBloqueo
    descripcion: str | None = Field(default=None, max_length=300)


class AgendaReservaOut(BaseModel):
    """One booking as the agenda board draws it."""

    reserva_id: int
    codigo: str
    estado: str
    inicio: datetime
    fin: datetime
    cliente: str
    placa: str
    servicio: str
    operario: str | None = None


class AgendaBahiaOut(BaseModel):
    bahia: BahiaResumen
    estado: str
    reservas: list[AgendaReservaOut] = Field(default_factory=list)
    bloqueos: list[BloqueoOut] = Field(default_factory=list)


class AgendaDiaOut(BaseModel):
    fecha: date
    laborable: bool
    motivo_no_laborable: str | None = None
    apertura: datetime | None = None
    cierre: datetime | None = None
    bahias: list[AgendaBahiaOut] = Field(default_factory=list)


class AgendaOut(BaseModel):
    """``GET /agenda``: one day, or the seven of its week (RF-018)."""

    vista: str
    desde: date
    hasta: date
    dias: list[AgendaDiaOut] = Field(default_factory=list)
