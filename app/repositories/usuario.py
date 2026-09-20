"""Data access for ``usuario``, ``rol`` and ``permiso``."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import Permiso, Rol, RolPermiso, Usuario


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


def existe_documento(db: Session, numero_documento: str) -> bool:
    """Whether the document number is already taken (RF-001 v1.0, step 4)."""
    consulta = select(Usuario.id).where(Usuario.numero_documento == numero_documento.strip())
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


def listar_paginado(
    db: Session,
    *,
    rol_id: int | None = None,
    estado_cuenta: str | None = None,
    pagina: int,
    tamanio: int,
) -> tuple[list[Usuario], int]:
    """One page of accounts for the administration screen (RF-035)."""
    filtros = []
    if rol_id is not None:
        filtros.append(Usuario.rol_id == rol_id)
    if estado_cuenta is not None:
        filtros.append(Usuario.estado_cuenta == estado_cuenta)

    total = db.scalar(select(func.count(Usuario.id)).where(*filtros)) or 0
    consulta = (
        select(Usuario)
        .where(*filtros)
        .options(selectinload(Usuario.rol).selectinload(Rol.permisos))
        .options(joinedload(Usuario.bahia_habitual))
        .order_by(Usuario.id)
        .limit(tamanio)
        .offset((pagina - 1) * tamanio)
    )
    return list(db.scalars(consulta).unique().all()), total


def listar_con_permiso(db: Session, codigo_permiso: str) -> list[Usuario]:
    """Accounts whose role grants a permission CODE (principle P5).

    RF-020 needs "the operators": it resolves them by the permission that makes
    somebody an operator (``reserva:avanzar_estado``), never by a role name, so
    a shop that invents a new role gets its people suggested for free.
    """
    consulta = (
        select(Usuario)
        .join(Rol, Rol.id == Usuario.rol_id)
        .join(RolPermiso, RolPermiso.rol_id == Rol.id)
        .join(Permiso, Permiso.id == RolPermiso.permiso_id)
        .where(Permiso.codigo == codigo_permiso)
        .options(selectinload(Usuario.rol).selectinload(Rol.permisos))
        .options(joinedload(Usuario.bahia_habitual))
        .order_by(Usuario.id)
    )
    return list(db.scalars(consulta).unique().all())


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
    bahia_habitual_id: int | None = None,
    tipo_documento: str | None = None,
    numero_documento: str | None = None,
    consentimiento_privacidad_en: datetime | None = None,
) -> Usuario:
    """Insert a user. The password arrives already hashed."""
    usuario = Usuario(
        nombres=nombres,
        apellidos=apellidos,
        correo=correo.strip().lower(),
        telefono=telefono,
        tipo_documento=tipo_documento,
        numero_documento=numero_documento,
        hash_password=hash_password,
        rol_id=rol_id,
        estado_cuenta=estado_cuenta,
        bahia_habitual_id=bahia_habitual_id,
        consentimiento_privacidad_en=consentimiento_privacidad_en,
        intentos_fallidos=0,
    )
    db.add(usuario)
    db.flush()
    return usuario
