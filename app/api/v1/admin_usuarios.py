"""Internal user administration (RF-035)."""

import math

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.deps import get_db, permisos_actuales, requiere_permiso
from app.models import Usuario
from app.schemas import (
    TAMANIO_PAGINA_DEFECTO,
    TAMANIO_PAGINA_MAXIMO,
    ErrorBody,
    Pagina,
    UsuarioInternoActualizar,
    UsuarioInternoCrear,
    UsuarioOut,
)
from app.services import usuario_service
from app.services.ensamblador import armar_usuario

router = APIRouter(prefix="/admin/usuarios", tags=["administración"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.get(
    "",
    response_model=Pagina[UsuarioOut],
    responses=RESPUESTAS,
    summary="Listar las cuentas, con filtro por rol y por estado",
)
def listar(
    rol_id: int | None = Query(default=None, ge=1),
    estado_cuenta: str | None = Query(default=None, max_length=30),
    pagina: int = Query(default=1, ge=1),
    tamanio: int = Query(default=TAMANIO_PAGINA_DEFECTO, ge=1, le=TAMANIO_PAGINA_MAXIMO),
    _: Usuario = Depends(requiere_permiso(usuario_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> Pagina[UsuarioOut]:
    items, total, pagina, tamanio = usuario_service.listar(
        db,
        rol_id=rol_id,
        estado_cuenta=estado_cuenta or None,
        pagina=pagina,
        tamanio=tamanio,
    )
    return Pagina[UsuarioOut](
        items=[armar_usuario(usuario) for usuario in items],
        pagina=pagina,
        tamanio=tamanio,
        total=total,
        total_paginas=math.ceil(total / tamanio) if total else 0,
    )


@router.post(
    "",
    response_model=UsuarioOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Dar de alta a un trabajador y enviarle su contraseña temporal",
)
def crear(
    datos: UsuarioInternoCrear,
    autor: Usuario = Depends(requiere_permiso(usuario_service.PERMISO_ADMINISTRAR)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> UsuarioOut:
    """RF-035: alta de un trabajador con su rol y su bahía habitual.

    Dar de alta con un rol es asignar un rol, así que el servicio exige
    además ``rol:administrar`` (RF-004).
    """
    return armar_usuario(usuario_service.crear(db, datos, autor, permisos=permisos))


@router.patch(
    "/{usuario_id}",
    response_model=UsuarioOut,
    responses=RESPUESTAS,
    summary="Editar, cambiar de rol, activar o desactivar una cuenta",
)
def actualizar(
    usuario_id: int,
    datos: UsuarioInternoActualizar,
    autor: Usuario = Depends(requiere_permiso(usuario_service.PERMISO_ADMINISTRAR)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> UsuarioOut:
    """RF-035: edición, cambio de rol y activación de una cuenta.

    ``usuario:administrar`` abre la puerta; **cambiar el rol exige además**
    ``rol:administrar``, y lo comprueba el servicio, no este router: es la
    misma escritura que hace ``PUT /admin/usuarios/{id}/rol`` y no puede
    depender de por dónde se entre (RF-004).
    """
    return armar_usuario(
        usuario_service.actualizar(db, usuario_id, datos, autor, permisos=permisos)
    )
