"""Role and permission administration (RF-004).

The module that assigns roles is, paradoxically, the one that must know least
about them (principle P5): a role is addressed by ID, the permissions it grants
are read from ``rol_permiso``, and the only permission code spelled here is the
one that guards this very administration. No role NAME is ever compared.

Every change writes a domain event with the previous and the new value, which
is what RF-004 CA-02 reads back from the audit trail, and triggers the refresh
token revocation hook of flow 4a.
"""

from sqlalchemy.orm import Session

from app.core.errors import CambioDeRolPropioDenegado, RecursoNoEncontrado, detalle
from app.models import Permiso, Rol, Usuario
from app.repositories import usuario as usuario_repo
from app.services import auth_service, eventos

#: The permission that guards this module. It is also the one an administrator
#: may not take away from themselves (flow 3a): losing it means losing the
#: ability to undo the change.
PERMISO_ADMINISTRAR_ROLES = "rol:administrar"


def listar_roles(db: Session) -> list[Rol]:
    """Every role with the permissions it grants (RF-004 step 1)."""
    return usuario_repo.listar_roles(db)


def listar_permisos(db: Session) -> list[Permiso]:
    """The permission catalogue, so the screen can explain what a role can do."""
    return usuario_repo.listar_permisos(db)


def aplicar_rol(db: Session, usuario: Usuario, rol: Rol, autor: Usuario) -> bool:
    """Move ``usuario`` to ``rol`` inside the CALLER's transaction.

    Split out of :func:`asignar_rol` so RF-035 can change a worker's role in the
    same write as the rest of their profile without committing twice. Returns
    whether anything changed; assigning the role the user already holds is a
    no-op that writes nothing to the audit trail and drops no session.
    """
    anterior = usuario.rol
    if anterior.id == rol.id:
        return False

    if usuario.id == autor.id and PERMISO_ADMINISTRAR_ROLES not in set(rol.codigos_permisos):
        raise CambioDeRolPropioDenegado(
            detalles=[
                detalle(
                    "rol_id",
                    f"El rol «{rol.nombre}» no incluye {PERMISO_ADMINISTRAR_ROLES}.",
                )
            ]
        )

    usuario.rol_id = rol.id

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario.id,
        eventos.USUARIO_ROL_CAMBIADO,
        autor_id=autor.id,
        datos={
            "rol_anterior_id": anterior.id,
            "rol_anterior": anterior.nombre,
            "rol_nuevo_id": rol.id,
            "rol_nuevo": rol.nombre,
        },
        # RF-036 lists "cambios de rol" among the sensitive operations, and a
        # change is audited with both sides (CA-01 states the rule using a
        # price, but the rule is about changes, not about prices).
        valor_anterior={"rol_id": anterior.id, "rol": anterior.nombre},
        valor_nuevo={"rol_id": rol.id, "rol": rol.nombre},
    )
    auth_service.revocar_tokens_de_refresco(db, usuario.id, motivo=eventos.USUARIO_ROL_CAMBIADO)
    return True


def obtener_rol(db: Session, rol_id: int) -> Rol:
    """Resolve a role by id, 404 when it does not exist."""
    rol = usuario_repo.obtener_rol_por_id(db, rol_id)
    if rol is None:
        raise RecursoNoEncontrado(
            "No encontramos ese rol. Consulta los roles disponibles antes de asignarlo.",
            detalles=[detalle("rol_id", "El rol no existe.")],
        )
    return rol


def asignar_rol(db: Session, usuario_id: int, rol_id: int, autor: Usuario) -> Usuario:
    """Give ``usuario_id`` the role ``rol_id`` (RF-004 steps 3 and 4).

    Two rules guard the change:

    * flow 3a - the caller may not leave themselves without
      ``rol:administrar``. The check is on the PERMISSION the destination role
      grants, not on its name, so it keeps working for any role the shop
      invents later;
    * flow 4a - a role change invalidates the refresh tokens of the affected
      user, so the new permissions apply on the next token instead of on the
      next login (:func:`app.services.auth_service.revocar_tokens_de_refresco`).

    Assigning the role the user already holds is a no-op: nothing is written to
    the audit trail and no session is dropped.
    """
    usuario = usuario_repo.obtener_por_id(db, usuario_id)
    if usuario is None:
        raise RecursoNoEncontrado("No encontramos ese usuario.")

    rol = obtener_rol(db, rol_id)
    if not aplicar_rol(db, usuario, rol, autor):
        return usuario

    db.commit()
    db.refresh(usuario)
    return usuario
