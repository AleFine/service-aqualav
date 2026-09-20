"""State catalogue (EXTENSION POINT P3).

Authenticated but not permission gated, like ``GET /auth/yo``: the list of
states is not sensitive and all three roles render screens from it. Which moves
a given caller may perform is a different question, answered per reservation by
``transiciones_permitidas``.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_db, usuario_actual
from app.models import Usuario
from app.schemas import ErrorBody, EstadoCatalogoOut, Lista
from app.services import estado_service

router = APIRouter(prefix="/estados", tags=["estados"])


@router.get(
    "",
    response_model=Lista[EstadoCatalogoOut],
    responses={401: {"model": ErrorBody}},
    summary="Catálogo de estados de reserva",
)
def listar(
    _: Usuario = Depends(usuario_actual),
    db: Session = Depends(get_db),
) -> Lista[EstadoCatalogoOut]:
    return Lista[EstadoCatalogoOut](items=estado_service.listar(db))
