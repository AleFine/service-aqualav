"""FastAPI dependencies: current user and permission gates.

Authorization is ALWAYS by permission code (contract section 3, principle P5).
Nothing here - and nothing anywhere outside ``app/seed.py`` - compares a role
name: RF-004 CA-03 is verified by inspecting the code, and ``tests/test_rbac.py``
automates that inspection.
"""

from collections.abc import Callable

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.errors import NoAutenticado, PermisoDenegado, detalle
from app.core.security import TIPO_ACCESO, decodificar_token
from app.database import get_db
from app.models import ESTADOS_CUENTA_CON_ACCESO, Usuario
from app.repositories import usuario as usuario_repo
from app.seed import PERMISOS

#: ``auto_error=False`` so a missing header reaches us and we can answer with
#: the uniform error body (401) instead of Starlette's bare 403.
esquema_bearer = HTTPBearer(auto_error=False, scheme_name="Bearer")


#: Re-exported verbatim (not wrapped) so ``app.dependency_overrides[get_db]``
#: works no matter which of the two modules the caller imported it from.
__all__ = [
    "esquema_bearer",
    "get_db",
    "permisos_actuales",
    "requiere_algun_permiso",
    "requiere_permiso",
    "usuario_actual",
]


def usuario_actual(
    credenciales: HTTPAuthorizationCredentials | None = Depends(esquema_bearer),
    db: Session = Depends(get_db),
) -> Usuario:
    """Resolve the caller from the access token (RF-004 flow 1-2).

    A missing, malformed, expired or wrong-type token is a 401; so is a token
    whose user no longer exists or whose account may no longer be used. An
    account still pending verification MAY be used: see
    ``ESTADOS_CUENTA_CON_ACCESO`` for why RF-001 flow 5a demands it.
    """
    if credenciales is None or not credenciales.credentials:
        raise NoAutenticado("Necesitas iniciar sesión para acceder a este recurso.")

    payload = decodificar_token(credenciales.credentials, TIPO_ACCESO)
    if payload is None:
        raise NoAutenticado()

    usuario = usuario_repo.obtener_por_id(db, payload.usuario_id)
    if usuario is None or usuario.estado_cuenta not in ESTADOS_CUENTA_CON_ACCESO:
        raise NoAutenticado()

    return usuario


def permisos_actuales(usuario: Usuario = Depends(usuario_actual)) -> list[str]:
    """The caller's permission codes.

    They are read from the database rather than from the token claims, so a
    role change takes effect on the next request instead of on the next login.
    """
    return usuario.rol.codigos_permisos


def _verificar(codigos_requeridos: tuple[str, ...], permisos: list[str]) -> bool:
    return bool(set(codigos_requeridos) & set(permisos or ()))


def requiere_permiso(codigo: str) -> Callable[..., Usuario]:
    """Dependency factory: the caller must hold ``codigo``.

    Answers 401 when there is no usable token (CA-02) and 403 when the token is
    valid but the permission is missing (CA-01). The code is checked against
    the seeded catalog when the app starts, so a typo fails at import time
    instead of silently rejecting every request.
    """
    if codigo not in PERMISOS:
        raise ValueError(f"Permiso desconocido: {codigo}")

    def dependencia(
        usuario: Usuario = Depends(usuario_actual),
        permisos: list[str] = Depends(permisos_actuales),
    ) -> Usuario:
        if codigo not in set(permisos or ()):
            raise PermisoDenegado(detalles=[detalle(None, f"Se requiere el permiso {codigo}.")])
        return usuario

    return dependencia


def requiere_algun_permiso(*codigos: str) -> Callable[..., Usuario]:
    """Same as :func:`requiere_permiso` but any one of ``codigos`` is enough.

    ``GET /reservas`` is reachable both with ``reserva:leer_propias`` and with
    ``reserva:leer_todas``; which rows come back is then decided by the
    horizontal filter inside ``reserva_service``.
    """
    desconocidos = [codigo for codigo in codigos if codigo not in PERMISOS]
    if desconocidos:
        raise ValueError(f"Permisos desconocidos: {', '.join(desconocidos)}")

    def dependencia(
        usuario: Usuario = Depends(usuario_actual),
        permisos: list[str] = Depends(permisos_actuales),
    ) -> Usuario:
        if not _verificar(codigos, permisos):
            raise PermisoDenegado(
                detalles=[detalle(None, f"Se requiere alguno de: {', '.join(codigos)}.")]
            )
        return usuario

    return dependencia
