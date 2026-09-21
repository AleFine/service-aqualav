"""The audit trail (RF-036, RNF-014).

RF-036 asks for a register of the sensitive operations - "autenticaciones,
cambios de precio, cambios de rol, cancelaciones, pagos y reembolsos" - that is
not modifiable, carries author, date, action and affected values, is queryable
with filters by user, event type and date, and can be exported.

**There is no ``bitacora_auditoria`` table.** The gap analysis calls
``evento_dominio`` "el ancestro de ``bitacora_auditoria``" and that is taken
literally: the log the MVP has been writing since day one IS the audit trail,
extended by INC-8 with ``valor_anterior``/``valor_nuevo`` and the three indexes
the filters need. The authentications are the one thing it never held, and
INC-3 has been writing those to ``intento_login`` - also unread until now - so
the trail is the union of the two (see ``app/repositories/auditoria.py``).

Choosing a view over a new table is worth defending, because the alternative
looks tidier:

* **the rollback of flow 2a comes for free and provably.** The audit row is
  appended inside the business transaction (``eventos.registrar_evento``), so
  "si falla el registro de auditoria la operacion principal se revierte" is a
  property of the code path, not a compensating action somebody must not
  forget. A separate table written after the fact could only be best-effort;
* **a copy of an append-only log can diverge from it**, and then there are two
  answers to "what happened" and no way to tell which is the record;
* **it is already insert-only.** Nothing in the codebase updates or deletes
  ``evento_dominio`` or ``intento_login``; CA-02 is enforced by absence, and
  ``tests/test_auditoria.py`` walks the package with ``ast`` to keep it so.

RNF-014's last clause - "sin contraseñas ni tokens ni tarjetas" - is honoured
twice: no caller ever puts one in an event (INC-4 has a test watching the PAN,
this increment has one watching the whole trail), and :func:`_ocultar` redacts
anything that looks like a credential on the way OUT, so a future event that
gets it wrong is not a breach of the screen as well.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.horario import a_lima, desde_bd
from app.models import TipoReporte
from app.repositories import auditoria as auditoria_repo
from app.repositories import autenticacion as autenticacion_repo
from app.repositories import evento as evento_repo
from app.repositories import usuario as usuario_repo
from app.services import eventos, reporte_service

#: The permission that guards the trail (principle P5).
PERMISO_LEER_AUDITORIA = "auditoria:leer"

#: What a redacted value reads as. Visible on purpose: the READER has to know
#: that something was there and was withheld, which is different from a field
#: that was never written.
OCULTO = "[oculto]"

#: Key fragments that may never leave this module (RNF-014). Matched as
#: substrings of the lowercased key, so ``token_hash``, ``numero_tarjeta`` and
#: ``password_temporal`` are all caught by one entry each. ``idempotency_key``
#: is deliberately NOT here: it is a reconciliation handle, not a credential,
#: and an audit trail that hides it cannot be reconciled against the gateway.
CLAVES_SENSIBLES = ("password", "contrasena", "contraseña", "token", "tarjeta", "cvv", "secret")

#: How many rows one export of the trail carries at most. A twelve-month
#: period is already the hard bound (flow 3a); this is the safety net that
#: keeps a pathological shop from loading a million rows into memory.
LIMITE_EXPORTACION = 20_000

#: The two projected authentication actions, re-exported so callers do not
#: have to know which module spells them. They must agree with the SQL
#: literals of the repository, and the assertion below is what says so.
ACCION_LOGIN_EXITOSO = eventos.USUARIO_AUTENTICACION_EXITOSA
ACCION_LOGIN_FALLIDO = eventos.USUARIO_AUTENTICACION_FALLIDA
assert ACCION_LOGIN_EXITOSO == auditoria_repo.ACCION_LOGIN_EXITOSO
assert ACCION_LOGIN_FALLIDO == auditoria_repo.ACCION_LOGIN_FALLIDO


@dataclass(frozen=True)
class Fila:
    """One entry of the trail, whatever table it came from."""

    fuente: str
    fuente_id: int
    entidad: str
    entidad_id: int | None
    accion: str
    autor_id: int | None
    autor: str | None
    ocurrido_en: datetime
    valor_anterior: dict[str, Any] | None = None
    valor_nuevo: dict[str, Any] | None = None
    detalle: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Pagina:
    """One page of the trail plus what a filter bar needs to draw itself."""

    filas: list[Fila]
    total: int
    pagina: int
    tamanio: int
    acciones: list[str]


# --------------------------------------------------------------------------
# RNF-014: nothing that is a credential leaves this module
# --------------------------------------------------------------------------
def _es_sensible(clave: str) -> bool:
    minuscula = str(clave).lower()
    return any(fragmento in minuscula for fragmento in CLAVES_SENSIBLES)


def _ocultar(valor: Any) -> Any:
    """Redact anything that looks like a credential, at any depth.

    A belt over the braces: no caller puts a password, a token or a card
    number into an event today, and a test asserts the whole trail is clean.
    This is what makes that still true the day somebody adds an event without
    reading RNF-014 first.
    """
    if isinstance(valor, dict):
        return {
            clave: (OCULTO if _es_sensible(clave) else _ocultar(contenido))
            for clave, contenido in valor.items()
        }
    if isinstance(valor, list):
        return [_ocultar(elemento) for elemento in valor]
    return valor


# --------------------------------------------------------------------------
# Reading the trail
# --------------------------------------------------------------------------
def _fila_de_evento(evento, autores: dict[int, Any]) -> Fila:
    autor = autores.get(evento.autor_id) if evento.autor_id is not None else None
    return Fila(
        fuente=auditoria_repo.FUENTE_EVENTO,
        fuente_id=evento.id,
        entidad=evento.entidad,
        entidad_id=evento.entidad_id,
        accion=evento.accion,
        autor_id=evento.autor_id,
        autor=autor.nombre_completo if autor else None,
        ocurrido_en=desde_bd(evento.ocurrido_en),
        valor_anterior=_ocultar(evento.valor_anterior) if evento.valor_anterior else None,
        valor_nuevo=_ocultar(evento.valor_nuevo) if evento.valor_nuevo else None,
        detalle=_ocultar(dict(evento.datos or {})),
    )


def _fila_de_intento(intento, autores: dict[int, Any]) -> Fila:
    """Project one ``intento_login`` row onto the shape of the trail.

    ``correo`` travels because it is the whole point of an authentication
    trail: an attempt against an address that does not exist is still an
    attempt, and it is exactly the shape of an enumeration attack. ``motivo``
    is an error CODE - INC-3 never stores a message and never the password.
    """
    autor = autores.get(intento.usuario_id) if intento.usuario_id is not None else None
    return Fila(
        fuente=auditoria_repo.FUENTE_INTENTO,
        fuente_id=intento.id,
        entidad=auditoria_repo.ENTIDAD_LOGIN,
        entidad_id=intento.usuario_id,
        accion=ACCION_LOGIN_EXITOSO if intento.exitoso else ACCION_LOGIN_FALLIDO,
        autor_id=intento.usuario_id,
        autor=autor.nombre_completo if autor else None,
        ocurrido_en=desde_bd(intento.ocurrido_en),
        valor_nuevo={"exitoso": bool(intento.exitoso)},
        detalle={"correo": intento.correo, "motivo": intento.motivo},
    )


def _hidratar(db: Session, claves: Iterable[auditoria_repo.Clave]) -> list[Fila]:
    """Turn a page of source/id pairs into full rows with three queries.

    The union gave the ORDER and the page; this gives the content. Doing it in
    two ``IN`` queries plus one for the authors is what keeps a page of fifty
    rows from becoming a hundred and fifty round trips.
    """
    lista = list(claves)
    if not lista:
        return []

    ids_evento = [c.fuente_id for c in lista if c.fuente == auditoria_repo.FUENTE_EVENTO]
    ids_intento = [c.fuente_id for c in lista if c.fuente == auditoria_repo.FUENTE_INTENTO]

    eventos_por_id = evento_repo.listar_por_ids(db, ids_evento)
    intentos_por_id = autenticacion_repo.listar_intentos_por_ids(db, ids_intento)

    autores_ids: set[int] = set()
    autores_ids.update(
        fila.autor_id for fila in eventos_por_id.values() if fila.autor_id is not None
    )
    autores_ids.update(
        fila.usuario_id for fila in intentos_por_id.values() if fila.usuario_id is not None
    )
    autores = usuario_repo.listar_por_ids(db, autores_ids)

    filas: list[Fila] = []
    for clave in lista:
        if clave.fuente == auditoria_repo.FUENTE_EVENTO:
            evento = eventos_por_id.get(clave.fuente_id)
            if evento is not None:
                filas.append(_fila_de_evento(evento, autores))
        else:
            intento = intentos_por_id.get(clave.fuente_id)
            if intento is not None:
                filas.append(_fila_de_intento(intento, autores))
    return filas


def _filtros(
    desde: date,
    hasta: date,
    *,
    usuario_id: int | None,
    accion: str | None,
    entidad: str | None,
    entidad_id: int | None,
) -> auditoria_repo.Filtros:
    reporte_service.validar_rango(desde, hasta)
    inicio, fin = reporte_service.limites_utc(desde, hasta)
    return auditoria_repo.Filtros(
        desde=inicio,
        hasta=fin,
        usuario_id=usuario_id,
        accion=(accion or "").strip() or None,
        entidad=(entidad or "").strip() or None,
        entidad_id=entidad_id,
    )


def consultar(
    db: Session,
    desde: date,
    hasta: date,
    *,
    usuario_id: int | None = None,
    accion: str | None = None,
    entidad: str | None = None,
    entidad_id: int | None = None,
    pagina: int = 1,
    tamanio: int = 20,
) -> Pagina:
    """One page of the trail (RF-036 "consulta paginada con filtros").

    The range is mandatory and bounded to twelve months, which is flow 3a
    ("consulta muy amplia -> se pide acotar el rango") enforced rather than
    suggested: a trail is only useful if asking about it cannot take the
    server down.
    """
    filtros = _filtros(
        desde,
        hasta,
        usuario_id=usuario_id,
        accion=accion,
        entidad=entidad,
        entidad_id=entidad_id,
    )

    total = auditoria_repo.contar(db, filtros)
    claves = auditoria_repo.listar(
        db, filtros, limite=tamanio, desplazamiento=(pagina - 1) * tamanio
    )
    return Pagina(
        filas=_hidratar(db, claves),
        total=total,
        pagina=pagina,
        tamanio=tamanio,
        acciones=list(auditoria_repo.acciones_registradas(db, filtros)),
    )


# --------------------------------------------------------------------------
# RF-036 "exportable": the same rows, in the shape the exporter understands
# --------------------------------------------------------------------------
COLUMNAS = (
    reporte_service.Columna("momento", "Fecha y hora"),
    reporte_service.Columna("autor", "Autor"),
    reporte_service.Columna("accion", "Acción"),
    reporte_service.Columna("entidad", "Entidad"),
    reporte_service.Columna("entidad_id", "Id"),
    reporte_service.Columna("valor_anterior", "Valor anterior"),
    reporte_service.Columna("valor_nuevo", "Valor nuevo"),
    reporte_service.Columna("detalle", "Detalle"),
)

FORMATO_FECHA = "%d/%m/%Y %H:%M:%S"


def _texto(valor: Any) -> str:
    """Flatten a JSON value into one cell. Empty when there is nothing."""
    if valor in (None, {}, []):
        return ""
    if isinstance(valor, str):
        return valor
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, default=str)


def reporte(
    db: Session,
    desde: date,
    hasta: date,
    *,
    usuario_id: int | None = None,
    accion: str | None = None,
    entidad: str | None = None,
    entidad_id: int | None = None,
) -> reporte_service.Reporte:
    """The trail as an exportable report (RF-036 "exportable").

    Same shape as the four reports of RF-034, so CSV and PDF are produced by
    the same exporter and the audit trail does not get a second, slightly
    different renderer that nobody remembers to fix.
    """
    filtros = _filtros(
        desde,
        hasta,
        usuario_id=usuario_id,
        accion=accion,
        entidad=entidad,
        entidad_id=entidad_id,
    )
    total = auditoria_repo.contar(db, filtros)
    claves = auditoria_repo.listar(db, filtros, limite=LIMITE_EXPORTACION, desplazamiento=0)

    filas = [
        {
            "momento": a_lima(fila.ocurrido_en).strftime(FORMATO_FECHA),
            "autor": fila.autor or "",
            "accion": fila.accion,
            "entidad": fila.entidad,
            "entidad_id": fila.entidad_id,
            "valor_anterior": _texto(fila.valor_anterior),
            "valor_nuevo": _texto(fila.valor_nuevo),
            "detalle": _texto(fila.detalle),
        }
        for fila in _hidratar(db, claves)
    ]

    return reporte_service.Reporte(
        tipo=TipoReporte.AUDITORIA.value,
        titulo="Bitácora de auditoría",
        desde=desde,
        hasta=hasta,
        columnas=COLUMNAS,
        filas=filas,
        totales={"registros": total, "exportados": len(filas)},
    )
