"""Role and permission administration (RF-004).

The MVP assigned roles "by migration or console" because there was no screen.
v1.0 adds one: list the roles with what they grant, list the permission
catalogue and assign a role to a user. All three are guarded by the permission
``rol:administrar``, never by a role name (principle P5).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import AsignacionRolIn, ErrorBody, Lista, PermisoOut, RolOut, UsuarioOut
from app.services import rol_service
from app.services.ensamblador import armar_permiso, armar_rol, armar_usuario

router = APIRouter(prefix="/admin", tags=["administración"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.get(
    "/roles",
    response_model=Lista[RolOut],
    responses=RESPUESTAS,
    summary="Listar los roles con sus permisos",
)
def listar_roles(
    _: Usuario = Depends(requiere_permiso(rol_service.PERMISO_ADMINISTRAR_ROLES)),
    db: Session = Depends(get_db),
) -> Lista[RolOut]:
    return Lista[RolOut](items=[armar_rol(rol) for rol in rol_service.listar_roles(db)])


@router.get(
    "/permisos",
    response_model=Lista[PermisoOut],
    responses=RESPUESTAS,
    summary="Listar el catálogo de permisos",
)
def listar_permisos(
    _: Usuario = Depends(requiere_permiso(rol_service.PERMISO_ADMINISTRAR_ROLES)),
    db: Session = Depends(get_db),
) -> Lista[PermisoOut]:
    return Lista[PermisoOut](
        items=[armar_permiso(permiso) for permiso in rol_service.listar_permisos(db)]
    )


@router.put(
    "/usuarios/{usuario_id}/rol",
    response_model=UsuarioOut,
    responses=RESPUESTAS,
    summary="Asignar el rol de un usuario",
)
def asignar_rol(
    usuario_id: int,
    datos: AsignacionRolIn,
    autor: Usuario = Depends(requiere_permiso(rol_service.PERMISO_ADMINISTRAR_ROLES)),
    db: Session = Depends(get_db),
) -> UsuarioOut:
    return armar_usuario(rol_service.asignar_rol(db, usuario_id, datos.rol_id, autor))
