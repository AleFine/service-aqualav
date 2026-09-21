"""Panel, report and export payloads (RF-033, RF-034)."""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import FormatoReporte, TipoReporte
from app.schemas.common import Dinero


class ColumnaOut(BaseModel):
    """One column of a report, so the app can draw a table it does not know.

    The reports are generic by design - five of them share one renderer - so
    the response describes its own shape instead of forcing the client to
    hardcode five column lists that drift from the server's.
    """

    clave: str
    titulo: str
    tipo: str


class ReporteOut(BaseModel):
    """A detailed report: its columns, its rows and its totals (RF-034).

    ``totales`` is the very dict the panel reads its cards from and the CSV
    prints at the end, which is what makes RF-033 CA-01 and RF-034 CA-01 the
    same guarantee instead of two coincidences.
    """

    tipo: str
    titulo: str
    desde: date
    hasta: date
    columnas: list[ColumnaOut] = Field(default_factory=list)
    filas: list[dict[str, Any]] = Field(default_factory=list)
    totales: dict[str, Any] = Field(default_factory=dict)
    #: RF-033 flow 3a: the cut-off these numbers are true as of.
    generado_en: datetime


class PuntoTendenciaOut(BaseModel):
    """One day of the trend chart (RF-033 "gráficos de tendencia")."""

    fecha: date
    servicios: int
    ingresos: Dinero


class TableroOut(BaseModel):
    """The indicator cards of RF-033.

    Every field here is read off a report, never recomputed, so CA-01 holds by
    construction. On an empty period every number is a zero and ``sin_datos``
    plus ``aviso`` carry the notice flow 2a asks for - CA-02 is "valores en
    cero SIN ERROR", so this is a 200 with zeros and never a 404.
    """

    desde: date
    hasta: date
    servicios_atendidos: int
    ingresos: Dinero
    ticket_promedio: Dinero
    ocupacion_porcentaje: float
    tiempo_promedio_min: int
    #: 0.0 when nobody rated in the period. The minimum score is one star, so
    #: a zero can only mean "no ratings", and ``calificaciones`` says so too.
    calificacion_media: float
    calificaciones: int
    operarios_activos: int
    sin_datos: bool
    aviso: str | None = None
    generado_en: datetime
    tendencia: list[PuntoTendenciaOut] = Field(default_factory=list)


class ExportacionIn(BaseModel):
    """Body of ``POST /reportes/exportaciones`` (RF-034, RF-036)."""

    tipo: TipoReporte
    formato: FormatoReporte
    desde: date
    hasta: date
    #: RF-036 filters, when the export is of the audit trail. Ignored by the
    #: four operational reports, which are filtered by period alone.
    usuario_id: int | None = None
    accion: str | None = Field(default=None, max_length=60)
    entidad: str | None = Field(default=None, max_length=40)
    entidad_id: int | None = None


class ExportacionOut(BaseModel):
    """One export request and how it ended (RF-034 flow 4a)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tipo: str
    formato: str
    estado: str
    filas: int
    filtros: dict[str, Any] = Field(default_factory=dict)
    solicitado_por: str | None = None
    solicitado_en: datetime
    generado_en: datetime | None = None
    error: str | None = None
    #: Where to download it, once there is something to download. ``None``
    #: while the export is pending, which is what tells the app to keep
    #: waiting instead of offering a broken link.
    archivo_url: str | None = None
