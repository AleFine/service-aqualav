"""Operative agenda: opening hours, holidays and slot blockings (RF-018).

RN-07 used to be three constants inside ``app/core/horario.py``, which is why
``es_laborable`` could only ever answer ``True``. The three tables here are the
data those constants become: the week the shop opens (``horario_atencion``),
the dates it does not (``dia_no_laborable``) and the slots a single bay - or
the whole shop - is unavailable (``bloqueo_franja``).

``app/core/horario.py`` stays pure: the service layer reads these rows, folds
them into a :class:`~app.core.horario.Calendario` and passes it down.
"""

from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Time, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.bahia import Bahia
    from app.models.usuario import Usuario


class HorarioAtencion(Base):
    """One weekday's opening window, versioned by ``vigente_desde`` (RN-07).

    The row in force for a date is the one with the greatest ``vigente_desde``
    not later than it; a weekday with no row, or whose row carries no hours,
    is closed. Versioning rather than updating keeps an old reservation
    explainable after the shop changes its hours (same reasoning as P6 for
    prices).
    """

    __tablename__ = "horario_atencion"
    __table_args__ = (Index("ix_horario_dia_vigencia", "dia_semana", "vigente_desde"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: ``datetime.weekday()``: Monday is 0, Sunday is 6.
    dia_semana: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Both NULL means the shop is closed that weekday from ``vigente_desde``.
    hora_apertura: Mapped[time | None] = mapped_column(Time, nullable=True)
    hora_cierre: Mapped[time | None] = mapped_column(Time, nullable=True)
    vigente_desde: Mapped[date] = mapped_column(Date, nullable=False)
    autor_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("usuario.id"), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class DiaNoLaborable(Base):
    """A date the shop does not open: a holiday, an inventory day (RF-018)."""

    __tablename__ = "dia_no_laborable"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, unique=True, index=True, nullable=False)
    motivo: Mapped[str] = mapped_column(String(200), nullable=False)
    autor_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("usuario.id"), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    autor: Mapped[Optional["Usuario"]] = relationship("Usuario", lazy="joined")


class BloqueoFranja(Base):
    """A slot in which a bay cannot take work (RF-018).

    ``bahia_id`` NULL blocks the WHOLE shop, which is how an "ausencia de
    personal" or an unexpected closure is expressed without inventing a row per
    bay. A shop-wide blocking that covers a full opening window also turns that
    date non-workable, so ``es_laborable`` answers it too (RF-018 CA-01).
    """

    __tablename__ = "bloqueo_franja"
    __table_args__ = (Index("ix_bloqueo_bahia_inicio", "bahia_id", "inicio"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bahia_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bahia.id"), index=True, nullable=True
    )
    inicio: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fin: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    motivo: Mapped[str] = mapped_column(String(30), nullable=False)
    descripcion: Mapped[str | None] = mapped_column(String(300), nullable=True)
    autor_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("usuario.id"), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    bahia: Mapped[Optional["Bahia"]] = relationship("Bahia", lazy="joined")
    autor: Mapped[Optional["Usuario"]] = relationship("Usuario", lazy="joined")
