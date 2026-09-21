"""The audit trail (RF-036, RNF-014).

**One verb.** This router declares ``GET`` and nothing else, and that is the
requirement rather than an omission: RF-036 CA-02 says that trying to edit a
record through the API must find that "la operación no existe o se deniega",
so there is no ``PUT``, no ``PATCH``, no ``DELETE`` and no ``POST`` here. A
request with any of them is answered by Starlette with 405 because the path
exists with other methods - "se deniega" - and a path that does not exist at
all answers 404 - "no existe". Both halves of CA-02 are true without a single
line of code enforcing them, which is the only way that kind of guarantee
survives a refactor.

Exporting the trail is not a second door either: it goes through
``POST /reportes/exportaciones`` with ``tipo=auditoria``, which is the same
exporter the four operational reports use, guarded by ``auditoria:leer``.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import TAMANIO_PAGINA_DEFECTO, TAMANIO_PAGINA_MAXIMO, BitacoraOut, ErrorBody
from app.services import auditoria_service
from app.services.ensamblador import armar_auditoria

router = APIRouter(prefix="/auditoria", tags=["auditoría"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.get(
    "",
    response_model=BitacoraOut,
    responses=RESPUESTAS,
    summary="Consultar la bitácora de auditoría",
)
def consultar(
    desde: date = Query(description="Primer día del periodo, inclusive."),
    hasta: date = Query(description="Último día del periodo, inclusive."),
    usuario_id: int | None = Query(default=None, description="Autor de la operación."),
    accion: str | None = Query(default=None, description="Tipo de evento."),
    entidad: str | None = Query(default=None, description="Entidad afectada."),
    entidad_id: int | None = Query(default=None, description="Id de la entidad afectada."),
    pagina: int = Query(default=1, ge=1),
    tamanio: int = Query(default=TAMANIO_PAGINA_DEFECTO, ge=1, le=TAMANIO_PAGINA_MAXIMO),
    _: Usuario = Depends(requiere_permiso(auditoria_service.PERMISO_LEER_AUDITORIA)),
    db: Session = Depends(get_db),
) -> BitacoraOut:
    """RF-036: consulta paginada con filtros por usuario, tipo de evento y fecha.

    `CA-01`: un cambio de precio figura con su valor anterior y el nuevo.
    Flujo `3a`: el rango es obligatorio y no puede superar los 12 meses; una
    consulta más amplia se rechaza pidiendo acotarla.
    """
    resultado = auditoria_service.consultar(
        db,
        desde,
        hasta,
        usuario_id=usuario_id,
        accion=accion,
        entidad=entidad,
        entidad_id=entidad_id,
        pagina=pagina,
        tamanio=tamanio,
    )
    total_paginas = (resultado.total + tamanio - 1) // tamanio
    return BitacoraOut(
        items=[armar_auditoria(fila) for fila in resultado.filas],
        pagina=resultado.pagina,
        tamanio=resultado.tamanio,
        total=resultado.total,
        total_paginas=total_paginas,
        acciones=resultado.acciones,
    )
