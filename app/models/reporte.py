"""Asynchronous report exports (RF-034 flow 4a).

RF-034 asks for four reports in two formats, and adds one sentence that needs a
table: "volumen elevado -> exportacion asincrona con notificacion al
finalizar". A request that is answered later is a request that has to be
remembered, so it is a row: what was asked, by whom, with which filters, how it
ended, and where the bytes landed.

``archivo_key`` is a KEY in the object store, never a path - the same contract
``comprobante`` uses. Where the file physically sits is the storage provider's
business, so moving it is a new implementation and not a migration.

``filtros`` is JSON for the same reason ``evento_dominio.datos`` is: the filter
set of a report is a document whose shape belongs to the report type, and
freezing it into columns would mean a migration every time a report grows a
filter. Keeping the exact filters is also what makes an export REPRODUCIBLE:
the scheduler rebuilds the report from this row alone, hours after the person
who asked for it logged out.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import EstadoExportacion

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.usuario import Usuario

CUERPO_JSON = JSON().with_variant(JSONB(), "postgresql")


class ReporteExportacion(Base):
    """One export request and how it ended (RF-034 flow 4a)."""

    __tablename__ = "reporte_exportacion"
    __table_args__ = (
        Index("ix_reporte_exportacion_estado_momento", "estado", "solicitado_en"),
        Index("ix_reporte_exportacion_solicitante", "solicitado_por_id", "solicitado_en"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: One of :class:`~app.models.enums.TipoReporte`.
    tipo: Mapped[str] = mapped_column(String(30), nullable=False)
    #: ``csv`` or ``pdf`` (:class:`~app.models.enums.FormatoReporte`).
    formato: Mapped[str] = mapped_column(String(10), nullable=False)
    filtros: Mapped[dict[str, Any]] = mapped_column(
        CUERPO_JSON, nullable=False, default=dict, server_default="{}"
    )
    estado: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EstadoExportacion.PENDIENTE.value,
        server_default=EstadoExportacion.PENDIENTE.value,
    )
    #: How many data rows the file carries. Written when it is generated, so a
    #: pending row honestly says it does not know yet.
    filas: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Key in the object store. NULL until the file exists.
    archivo_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: Why the generation failed, when it did. What the administrator reads.
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    solicitado_por_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("usuario.id"), index=True, nullable=False
    )
    solicitado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    generado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    solicitado_por: Mapped[Optional["Usuario"]] = relationship("Usuario", lazy="joined")

    @property
    def nombre_archivo(self) -> str:
        """What the browser saves it as: ``servicios-000012.csv``."""
        return f"{self.tipo}-{self.id:06d}.{self.formato}"
