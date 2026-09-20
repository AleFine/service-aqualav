"""Domain event log (EXTENSION POINT P7).

Nothing in the MVP reads this table. That is intentional: it is the seed of the
future notification / audit feature.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.usuario import Usuario

# JSONB on PostgreSQL, plain JSON everywhere else (e.g. SQLite in tests).
TipoDatos = JSON().with_variant(JSONB(), "postgresql")


class EventoDominio(Base):
    __tablename__ = "evento_dominio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entidad: Mapped[str] = mapped_column(String(40), nullable=False)
    entidad_id: Mapped[int] = mapped_column(Integer, nullable=False)
    accion: Mapped[str] = mapped_column(String(60), nullable=False)
    autor_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("usuario.id"), nullable=True)
    datos: Mapped[dict[str, Any]] = mapped_column(
        TipoDatos, nullable=False, default=dict, server_default="{}"
    )
    ocurrido_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    autor: Mapped[Optional["Usuario"]] = relationship("Usuario")
