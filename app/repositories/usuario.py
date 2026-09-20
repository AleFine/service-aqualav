"""Data access for ``usuario``, ``rol`` and ``permiso``."""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Rol, Usuario


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
