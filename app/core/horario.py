"""RN-07 opening hours, evaluated in America/Lima.

Monday to Saturday 08:00-19:00, Sunday 09:00-14:00. There is no closed day in
the MVP, so :func:`es_laborable` is always true; the function exists because
RF-013 has to answer ``laborable`` and v0.2 may introduce holidays.
"""

from datetime import UTC, date, datetime, time
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


def ventana_del_dia(fecha: date) -> tuple[datetime, datetime] | None:
    """Return the ``(apertura, cierre)`` instants for a date, or ``None`` if closed.

    Both ends are timezone-aware in America/Lima. The interval is treated as
    half-open ``[apertura, cierre)`` by the availability algorithm.
    """
    if fecha.weekday() == DOMINGO:
        apertura, cierre = APERTURA_DOMINGO, CIERRE_DOMINGO
    else:
        apertura, cierre = APERTURA_SEMANA, CIERRE_SEMANA

    return (
        datetime.combine(fecha, apertura, tzinfo=ZONA_LIMA),
        datetime.combine(fecha, cierre, tzinfo=ZONA_LIMA),
    )


def es_laborable(fecha: date) -> bool:
    """Whether the shop opens on that date."""
    return ventana_del_dia(fecha) is not None


def dentro_de_horario(inicio: datetime, fin: datetime) -> bool:
    """Whether the whole ``[inicio, fin)`` interval fits in one day's window."""
    inicio_lima = a_lima(inicio)
    fin_lima = a_lima(fin)

    if fin_lima <= inicio_lima:
        return False

    ventana = ventana_del_dia(inicio_lima.date())
    if ventana is None:
        return False

    apertura, cierre = ventana
    return apertura <= inicio_lima and fin_lima <= cierre
