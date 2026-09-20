"""The customer's own profile (RF-006)."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_db, usuario_actual
from app.models import Usuario
from app.schemas import ErrorBody, PerfilActualizar, PerfilOut
from app.services import perfil_service
from app.services.ensamblador import armar_usuario
from app.services.perfil_service import Perfil

router = APIRouter(prefix="/perfil", tags=["perfil"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


def _perfil(perfil: Perfil) -> PerfilOut:
    return PerfilOut(
        usuario=armar_usuario(perfil.usuario),
        correo_pendiente=perfil.correo_pendiente,
        verificacion_pendiente=perfil.verificacion_pendiente,
    )


@router.get(
    "",
    response_model=PerfilOut,
    responses=RESPUESTAS,
    summary="Consultar el perfil propio",
)
def obtener(
    usuario: Usuario = Depends(usuario_actual),
    db: Session = Depends(get_db),
) -> PerfilOut:
    """The profile belongs to whoever is holding the token: no permission gate.

    Same shape as ``GET /auth/yo`` and ``GET /estados`` - a permission code
    exists to separate what different people may do to OTHERS' data, and there
    is only one person who may read this.
    """
    return _perfil(perfil_service.obtener(db, usuario))


@router.patch(
    "",
    response_model=PerfilOut,
    responses=RESPUESTAS,
    summary="Editar el perfil propio",
)
def actualizar(
    datos: PerfilActualizar,
    usuario: Usuario = Depends(usuario_actual),
    db: Session = Depends(get_db),
) -> PerfilOut:
    """RF-006. A ``correo`` is REQUESTED, not applied: see CA-02."""
    return _perfil(perfil_service.actualizar(db, usuario, datos))
