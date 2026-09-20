"""RN-07 opening hours, evaluated in America/Lima.

Monday to Saturday 08:00-19:00, Sunday 09:00-14:00 - but only as the FALLBACK
calendar. RF-018 moves the opening hours and the holidays to data
(``horario_atencion`` and ``dia_no_laborable``), and this module stays pure:
it never opens a session. The service layer reads the rows, builds a
:class:`Calendario` and threads it through, exactly like the set of active
states is resolved once and passed down to the availability algorithm.

That is why :func:`es_laborable` finally means something: a date is workable
when the calendar declares a window for its weekday AND no holiday or
shop-wide closure covers it.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from types import MappingProxyType
from zoneinfo import ZoneInfo

from app.config import settings

ZONA_LIMA = ZoneInfo(settings.zona_horaria)

APERTURA_SEMANA = time(8, 0)
CIERRE_SEMANA = time(19, 0)
APERTURA_DOMINGO = time(9, 0)
CIERRE_DOMINGO = time(14, 0)

DOMINGO = 6  # datetime.weekday(): Monday is 0, Sunday is 6.

# Minutes between two consecutive candidate start times (RF-013 step 2).
PASO_BLOQUE_MIN = 15

#: The RN-07 week, as the seed writes it into ``horario_atencion``.
TRAMOS_RN07: dict[int, tuple[time, time]] = {
    **{dia: (APERTURA_SEMANA, CIERRE_SEMANA) for dia in range(DOMINGO)},
    DOMINGO: (APERTURA_DOMINGO, CIERRE_DOMINGO),
}


@dataclass(frozen=True)
class Calendario:
    """When the shop opens, as DATA (RF-018, RN-07).

    ``tramos`` maps ``datetime.weekday()`` to the ``(apertura, cierre)`` pair
    of that weekday; a weekday with no entry is closed. ``feriados`` maps a
    specific date to the reason it is not workable - holidays, and also the
    shop-wide blockings that cover a whole opening window.
    """

    tramos: Mapping[int, tuple[time, time]] = field(default_factory=dict)
    feriados: Mapping[date, str] = field(default_factory=dict)

    def motivo_no_laborable(self, fecha: date) -> str | None:
        """Why the shop does not open that date, or ``None`` when it does."""
        motivo = self.feriados.get(fecha)
        if motivo is not None:
            return motivo
        if fecha.weekday() not in self.tramos:
            return "El local no atiende ese día de la semana."
        return None


#: Calendar used when no row has been read: the literal RN-07 week. It keeps
#: every caller that does not care about holidays working unchanged, and it is
#: what the API falls back to if ``horario_atencion`` is ever empty.
CALENDARIO_PREDETERMINADO = Calendario(
    tramos=MappingProxyType(dict(TRAMOS_RN07)),
    feriados=MappingProxyType({}),
)


def ahora() -> datetime:
    """Current instant as a timezone-aware datetime in America/Lima."""
    return datetime.now(tz=ZONA_LIMA)


def ahora_utc() -> datetime:
    """Current instant in UTC.

    Every datetime the service layer writes to the database goes through this
    function (or :func:`a_utc`), so a column read back without an offset -
    which is what SQLite does with ``TIMESTAMP WITH TIME ZONE`` - is always UTC.
    """
    return datetime.now(tz=UTC)


def a_utc(momento: datetime) -> datetime:
    """Convert any datetime to UTC; a naive input is assumed to be UTC already."""
    if momento.tzinfo is None:
        return momento.replace(tzinfo=UTC)
    return momento.astimezone(UTC)


def desde_bd(momento: datetime | None) -> datetime | None:
    """Normalize a value read from the database into an aware UTC datetime.

    PostgreSQL returns ``timestamptz`` columns already aware. SQLite, used by
    the test suite, drops the offset and hands back a naive value; since every
    write is UTC, attaching UTC restores the original instant.
    """
    if momento is None:
        return None
    return a_utc(momento)


def a_lima(momento: datetime) -> datetime:
    """Convert any datetime to America/Lima; naive input is assumed to be Lima."""
    if momento.tzinfo is None:
        return momento.replace(tzinfo=ZONA_LIMA)
    return momento.astimezone(ZONA_LIMA)


def ventana_del_dia(
    fecha: date,
    calendario: Calendario = CALENDARIO_PREDETERMINADO,
) -> tuple[datetime, datetime] | None:
    """Return the ``(apertura, cierre)`` instants for a date, or ``None`` if closed.

    Both ends are timezone-aware in America/Lima. The interval is treated as
    half-open ``[apertura, cierre)`` by the availability algorithm.
    """
    if calendario.motivo_no_laborable(fecha) is not None:
        return None

    apertura, cierre = calendario.tramos[fecha.weekday()]
    if cierre <= apertura:  # pragma: no cover - guarded when the row is written
        return None

    return (
        datetime.combine(fecha, apertura, tzinfo=ZONA_LIMA),
        datetime.combine(fecha, cierre, tzinfo=ZONA_LIMA),
    )


def es_laborable(fecha: date, calendario: Calendario = CALENDARIO_PREDETERMINADO) -> bool:
    """Whether the shop opens on that date (RF-018 CA-01).

    Holidays and shop-wide closures travel inside ``calendario``, so this is no
    longer the constant ``True`` the MVP left behind.
    """
    return calendario.motivo_no_laborable(fecha) is None


def dentro_de_horario(
    inicio: datetime,
    fin: datetime,
    calendario: Calendario = CALENDARIO_PREDETERMINADO,
) -> bool:
    """Whether the whole ``[inicio, fin)`` interval fits in one day's window."""
    inicio_lima = a_lima(inicio)
    fin_lima = a_lima(fin)

    if fin_lima <= inicio_lima:
        return False

    ventana = ventana_del_dia(inicio_lima.date(), calendario)
    if ventana is None:
        return False

    apertura, cierre = ventana
    return apertura <= inicio_lima and fin_lima <= cierre
