"""Data access for ``usuario``, ``rol`` and ``permiso``."""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Permiso, Rol, Usuario


def obtener_por_id(db: Session, usuario_id: int) -> Usuario | None:
    """Load one user with its role and permissions already attached."""
    consulta = (
        select(Usuario)
        .where(Usuario.id == usuario_id)
        .options(selectinload(Usuario.rol).selectinload(Rol.permisos))
    )
    return db.scalars(consulta).first()


def obtener_por_correo(db: Session, correo: str) -> Usuario | None:
    """Look a user up by e-mail. The comparison is case insensitive."""
    consulta = (
        select(Usuario)
        .where(Usuario.correo == correo.strip().lower())
        .options(selectinload(Usuario.rol).selectinload(Rol.permisos))
    )
    return db.scalars(consulta).first()


def existe_correo(db: Session, correo: str) -> bool:
    """Whether the e-mail is already taken (RF-001 CA-02)."""
    consulta = select(Usuario.id).where(Usuario.correo == correo.strip().lower())
    return db.scalars(consulta).first() is not None


def obtener_rol(db: Session, nombre: str) -> Rol | None:
    """Resolve a role by its natural key. The caller supplies the name."""
    consulta = select(Rol).where(Rol.nombre == nombre).options(selectinload(Rol.permisos))
    return db.scalars(consulta).first()


def obtener_rol_por_id(db: Session, rol_id: int) -> Rol | None:
    """Resolve a role by id, with its permissions attached (RF-004 step 3).

    The role administration addresses a role by id on purpose: the payload of
    ``PUT /admin/usuarios/{id}/rol`` therefore never carries a role NAME, so no
    client can grow a branch on one either (principle P5).
    """
    consulta = select(Rol).where(Rol.id == rol_id).options(selectinload(Rol.permisos))
    return db.scalars(consulta).first()


def listar_roles(db: Session) -> list[Rol]:
    """Every role with its permissions, for the administration screen (RF-004)."""
    consulta = select(Rol).options(selectinload(Rol.permisos)).order_by(Rol.id)
    return list(db.scalars(consulta).all())


def listar_permisos(db: Session) -> list[Permiso]:
    """Every permission code the system knows about (RF-004 step 2)."""
    return list(db.scalars(select(Permiso).order_by(Permiso.codigo)).all())


def crear(
    db: Session,
    *,
    nombres: str,
    apellidos: str,
    correo: str,
    telefono: str,
    hash_password: str,
    rol_id: int,
    estado_cuenta: str,
) -> Usuario:
    """Insert a user. The password arrives already hashed."""
    usuario = Usuario(
        nombres=nombres,
        apellidos=apellidos,
        correo=correo.strip().lower(),
        telefono=telefono,
        hash_password=hash_password,
        rol_id=rol_id,
        estado_cuenta=estado_cuenta,
        intentos_fallidos=0,
    )
    db.add(usuario)
    db.flush()
    return usuario
