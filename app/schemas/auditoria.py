"""Audit trail payloads (RF-036, RNF-014).

There is no ``AuditoriaIn``, and there never will be: RF-036 CA-02 says that
trying to edit a record through the API must not be possible, so the audit
resource has exactly one verb and no request body anywhere in this module.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import Pagina


class AuditoriaOut(BaseModel):
    """One entry of the trail (RF-036 "autor, fecha, hora, acción y valores").

    ``fuente`` says which table the row came from - the domain event log or
    the authentication trail - because an auditor is entitled to know where a
    statement is being read from rather than being handed a merged list that
    hides its own provenance.
    """

    fuente: str
    id: int
    ocurrido_en: datetime
    accion: str
    entidad: str
    entidad_id: int | None = None
    autor_id: int | None = None
    autor: str | None = None
    #: RF-036 CA-01: a change shows both sides. ``None`` on an event that is
    #: an occurrence rather than a change - a payment replaced nothing.
    valor_anterior: dict[str, Any] | None = None
    valor_nuevo: dict[str, Any] | None = None
    #: The rest of the event payload. Never a password, a token or a card
    #: (RNF-014): nothing writes one, and the service redacts anything that
    #: looks like one on the way out.
    detalle: dict[str, Any] = Field(default_factory=dict)


class BitacoraOut(Pagina[AuditoriaOut]):
    """A page of the trail plus the actions present in the period.

    ``acciones`` is derived from the trail itself rather than from a hand
    written catalogue - the same reasoning that makes ``GET /estados`` derive
    its catalogue from ``transicion_estado`` (P3): an increment that starts
    writing a new action gets it into the filter for free.
    """

    acciones: list[str] = Field(default_factory=list)
