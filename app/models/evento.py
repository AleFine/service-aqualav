"""Domain event log (EXTENSION POINT P7), and the audit trail of RF-036.

The MVP wrote this table and read nothing back. INC-6 was the first consumer
(``reserva.calificacion_habilitada``), and INC-8 is what the table was always
for: the gap analysis calls ``evento_dominio`` "el ancestro de
``bitacora_auditoria``", and it is taken literally here. There is no second
audit table.

That is a decision, not a shortcut, and it rests on three things:

* **it is already written inside the business transaction.**
  ``eventos.registrar_evento`` flushes in the caller's unit of work, so if the
  audit row cannot be written the operation it describes is never committed.
  RF-036 flow 2a ("si falla el registro de auditoria, la operacion principal se
  revierte") is therefore a PROPERTY of where the row is written, not a
  compensating action somebody has to remember to code;
* **a second table would be a copy**, and a copy of an append-only log is a log
  that can disagree with itself. Every sensitive operation RF-036 lists -
  authentications, price changes, role changes, cancellations, payments and
  refunds - already writes here (P7) or into ``intento_login``;
* **it is append only already.** ``app/repositories/evento.py`` exposes
  ``crear`` and three readers, and nothing anywhere issues an UPDATE or a
  DELETE against it (RF-036 CA-02, RNF-014).

What INC-8 adds is exactly what the gap analysis prescribed: ``valor_anterior``
and ``valor_nuevo``, so a change shows both sides without the reader having to
parse a payload it did not design, plus the three indexes the RF-036 filters
need (author, action, date).
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.usuario import Usuario

# JSONB on PostgreSQL, plain JSON everywhere else (e.g. SQLite in tests).
TipoDatos = JSON().with_variant(JSONB(), "postgresql")


class EventoDominio(Base):
    __tablename__ = "evento_dominio"
    __table_args__ = (
        # The three filters RF-036 asks for: "por usuario, tipo de evento y
        # fecha". The date is part of two of them because every audit query is
        # bounded by a period (flow 3a) and none of them is ever unordered.
        Index("ix_evento_dominio_autor_momento", "autor_id", "ocurrido_en"),
        Index("ix_evento_dominio_accion_momento", "accion", "ocurrido_en"),
        Index("ix_evento_dominio_entidad_momento", "entidad", "ocurrido_en"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entidad: Mapped[str] = mapped_column(String(40), nullable=False)
    entidad_id: Mapped[int] = mapped_column(Integer, nullable=False)
    accion: Mapped[str] = mapped_column(String(60), nullable=False)
    autor_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("usuario.id"), nullable=True)
    datos: Mapped[dict[str, Any]] = mapped_column(
        TipoDatos, nullable=False, default=dict, server_default="{}"
    )
    #: RF-036 "valores afectados" and CA-01 ("figura el valor anterior y el
    #: nuevo"). Columns of their own rather than two conventional keys inside
    #: ``datos``: the audit screen renders a BEFORE and an AFTER for every kind
    #: of change, and a screen cannot be asked to know that a price change
    #: spells them ``monto_anterior``/``monto_nuevo`` while a role change
    #: spells them ``rol_anterior``/``rol_nuevo``. NULL on an event that is an
    #: occurrence rather than a change - a payment did not replace anything.
    valor_anterior: Mapped[dict[str, Any] | None] = mapped_column(TipoDatos, nullable=True)
    valor_nuevo: Mapped[dict[str, Any] | None] = mapped_column(TipoDatos, nullable=True)
    ocurrido_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    autor: Mapped[Optional["Usuario"]] = relationship("Usuario")
