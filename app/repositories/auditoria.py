"""The audit trail query (RF-036).

RF-036 asks for "consulta paginada con filtros por usuario, tipo de evento y
fecha" over "autenticaciones, cambios de precio, cambios de rol, cancelaciones,
pagos y reembolsos". Five of those six already live in ``evento_dominio`` (P7).
The sixth does not: INC-3 records every authentication attempt - successful or
not, with the error code and never the password - in ``intento_login``, and
nothing has ever read it back.

So the trail is the UNION of the two tables, and that is the whole reason this
module exists. Copying the login attempts into ``evento_dominio`` would have
been easier to query and wrong: it would create a second, divergeable record of
a fact one table already owns, and the copy could be written while the original
was not.

The union projects both sides onto the same seven columns - the ones the
filters, the ordering and the pagination need - and the service hydrates the
page afterwards with two ``IN`` queries. That keeps ``LIMIT``/``OFFSET`` and
``COUNT`` honest across both sources: merging two already-paginated lists in
Python cannot produce a correct page and cannot produce a correct total.

Nothing here writes. The trail is insert only (RNF-014, RF-036 CA-02).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, case, func, literal, select
from sqlalchemy.orm import Session

from app.models import EventoDominio, IntentoLogin

#: Which table a row of the trail came from.
FUENTE_EVENTO = "evento"
FUENTE_INTENTO = "intento_login"

#: The two action names ``intento_login`` is projected under. They live in
#: ``app.services.eventos`` as constants; they are repeated here as the SQL
#: literals of the projection, and the service asserts they agree.
ACCION_LOGIN_EXITOSO = "usuario.autenticacion_exitosa"
ACCION_LOGIN_FALLIDO = "usuario.autenticacion_fallida"
ACCIONES_LOGIN = (ACCION_LOGIN_EXITOSO, ACCION_LOGIN_FALLIDO)

#: Entity every authentication is filed under.
ENTIDAD_LOGIN = "usuario"


@dataclass(frozen=True)
class Clave:
    """Where one row of the page came from, so the service can hydrate it."""

    fuente: str
    fuente_id: int


@dataclass(frozen=True)
class Filtros:
    """Everything RF-036 lets an administrator narrow the trail by."""

    desde: datetime
    hasta: datetime
    usuario_id: int | None = None
    accion: str | None = None
    entidad: str | None = None
    entidad_id: int | None = None


def _consulta_eventos(filtros: Filtros) -> Select | None:
    """The ``evento_dominio`` half of the union, or ``None`` when filtered out."""
    if filtros.accion in ACCIONES_LOGIN:
        # Those two actions only exist as a projection of the other table.
        return None

    consulta = select(
        literal(FUENTE_EVENTO).label("fuente"),
        EventoDominio.id.label("fuente_id"),
        EventoDominio.entidad.label("entidad"),
        EventoDominio.entidad_id.label("entidad_id"),
        EventoDominio.accion.label("accion"),
        EventoDominio.autor_id.label("autor_id"),
        EventoDominio.ocurrido_en.label("ocurrido_en"),
    ).where(
        EventoDominio.ocurrido_en >= filtros.desde,
        EventoDominio.ocurrido_en < filtros.hasta,
    )

    if filtros.usuario_id is not None:
        consulta = consulta.where(EventoDominio.autor_id == filtros.usuario_id)
    if filtros.accion:
        consulta = consulta.where(EventoDominio.accion == filtros.accion)
    if filtros.entidad:
        consulta = consulta.where(EventoDominio.entidad == filtros.entidad)
    if filtros.entidad_id is not None:
        consulta = consulta.where(EventoDominio.entidad_id == filtros.entidad_id)
    return consulta


def _consulta_intentos(filtros: Filtros) -> Select | None:
    """The ``intento_login`` half, projected onto the same seven columns."""
    if filtros.entidad and filtros.entidad != ENTIDAD_LOGIN:
        return None
    if filtros.accion and filtros.accion not in ACCIONES_LOGIN:
        return None

    accion = case(
        (IntentoLogin.exitoso, literal(ACCION_LOGIN_EXITOSO)),
        else_=literal(ACCION_LOGIN_FALLIDO),
    )

    consulta = select(
        literal(FUENTE_INTENTO).label("fuente"),
        IntentoLogin.id.label("fuente_id"),
        literal(ENTIDAD_LOGIN).label("entidad"),
        IntentoLogin.usuario_id.label("entidad_id"),
        accion.label("accion"),
        IntentoLogin.usuario_id.label("autor_id"),
        IntentoLogin.ocurrido_en.label("ocurrido_en"),
    ).where(
        IntentoLogin.ocurrido_en >= filtros.desde,
        IntentoLogin.ocurrido_en < filtros.hasta,
    )

    if filtros.usuario_id is not None:
        consulta = consulta.where(IntentoLogin.usuario_id == filtros.usuario_id)
    if filtros.entidad_id is not None:
        consulta = consulta.where(IntentoLogin.usuario_id == filtros.entidad_id)
    if filtros.accion == ACCION_LOGIN_EXITOSO:
        consulta = consulta.where(IntentoLogin.exitoso.is_(True))
    elif filtros.accion == ACCION_LOGIN_FALLIDO:
        consulta = consulta.where(IntentoLogin.exitoso.is_(False))
    return consulta


def _partes(filtros: Filtros) -> list[Select]:
    """The halves of the union that survive the filters.

    ``is not None`` and never a truth test: a SQLAlchemy ``Select`` refuses to
    be evaluated as a boolean, and rightly so - the answer would be about the
    query object rather than about its rows.
    """
    candidatas = (_consulta_eventos(filtros), _consulta_intentos(filtros))
    return [parte for parte in candidatas if parte is not None]


def contar(db: Session, filtros: Filtros) -> int:
    """How many rows of the trail match, across both sources."""
    partes = _partes(filtros)
    if not partes:
        return 0
    union = partes[0] if len(partes) == 1 else partes[0].union_all(*partes[1:])
    return int(db.scalar(select(func.count()).select_from(union.subquery("bitacora"))) or 0)


def listar(db: Session, filtros: Filtros, *, limite: int, desplazamiento: int) -> list[Clave]:
    """One page of the trail, newest first, as source/id pairs.

    Ordering is ``(ocurrido_en, fuente, fuente_id)`` descending. The last two
    are the tie-break: ids repeat across the two tables, so without naming the
    source the order of two rows written in the same instant would depend on
    whatever the database felt like, and a page boundary would then be able to
    show or hide a row at random.
    """
    partes = _partes(filtros)
    if not partes:
        return []

    union = (partes[0] if len(partes) == 1 else partes[0].union_all(*partes[1:])).subquery(
        "bitacora"
    )
    consulta = (
        select(union.c.fuente, union.c.fuente_id)
        .order_by(union.c.ocurrido_en.desc(), union.c.fuente.desc(), union.c.fuente_id.desc())
        .limit(limite)
        .offset(desplazamiento)
    )
    return [Clave(fuente=fila[0], fuente_id=fila[1]) for fila in db.execute(consulta).all()]


def acciones_registradas(db: Session, filtros: Filtros) -> Sequence[str]:
    """The distinct actions present in the period, so a screen can offer them.

    The filter list of RF-036 ("tipo de evento") is derived from the trail
    itself rather than from a hand written catalogue, exactly like the state
    catalogue is derived from ``transicion_estado`` (P3): an increment that
    starts writing a new action gets it in the filter for free.
    """
    consulta = (
        select(EventoDominio.accion)
        .where(
            EventoDominio.ocurrido_en >= filtros.desde,
            EventoDominio.ocurrido_en < filtros.hasta,
        )
        .distinct()
    )
    acciones = set(db.scalars(consulta).all())

    exitosos = db.scalars(
        select(IntentoLogin.id)
        .where(
            IntentoLogin.ocurrido_en >= filtros.desde,
            IntentoLogin.ocurrido_en < filtros.hasta,
            IntentoLogin.exitoso.is_(True),
        )
        .limit(1)
    ).first()
    if exitosos is not None:
        acciones.add(ACCION_LOGIN_EXITOSO)

    fallidos = db.scalars(
        select(IntentoLogin.id)
        .where(
            IntentoLogin.ocurrido_en >= filtros.desde,
            IntentoLogin.ocurrido_en < filtros.hasta,
            IntentoLogin.exitoso.is_(False),
        )
        .limit(1)
    ).first()
    if fallidos is not None:
        acciones.add(ACCION_LOGIN_FALLIDO)

    return sorted(acciones)
