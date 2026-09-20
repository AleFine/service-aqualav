"""Data access for the credential tables (RF-001, RF-003, RF-005, RF-006).

Queries only: whether an expired token means "ask for another one" or "this
never existed" is a decision, and decisions live in the service layer.
"""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import IntentoLogin, TokenRecuperacion, TokenRefresco, VerificacionCorreo


# --------------------------------------------------------------------------
# E-mail verification (RF-001, RF-006)
# --------------------------------------------------------------------------
def crear_verificacion(
    db: Session, *, usuario_id: int, correo: str, token_hash: str, expira_en: datetime
) -> VerificacionCorreo:
    fila = VerificacionCorreo(
        usuario_id=usuario_id, correo=correo, token_hash=token_hash, expira_en=expira_en
    )
    db.add(fila)
    db.flush()
    return fila


def obtener_verificacion(db: Session, token_hash: str) -> VerificacionCorreo | None:
    consulta = select(VerificacionCorreo).where(VerificacionCorreo.token_hash == token_hash)
    return db.scalars(consulta).first()


def obtener_verificacion_viva(
    db: Session, usuario_id: int, momento: datetime
) -> VerificacionCorreo | None:
    """The newest verification of an account that is neither spent nor expired."""
    consulta = (
        select(VerificacionCorreo)
        .where(
            VerificacionCorreo.usuario_id == usuario_id,
            VerificacionCorreo.verificado_en.is_(None),
            VerificacionCorreo.expira_en > momento,
        )
        .order_by(VerificacionCorreo.id.desc())
    )
    return db.scalars(consulta).first()


def anular_verificaciones_pendientes(db: Session, usuario_id: int, momento: datetime) -> int:
    """Retire every unconsumed verification of one account.

    Asking for a new link has to invalidate the previous one: two live tokens
    for the same account means the older mail can still change an address the
    person already changed their mind about.
    """
    resultado = db.execute(
        update(VerificacionCorreo)
        .where(
            VerificacionCorreo.usuario_id == usuario_id,
            VerificacionCorreo.verificado_en.is_(None),
            VerificacionCorreo.expira_en > momento,
        )
        .values(expira_en=momento)
    )
    return int(resultado.rowcount or 0)


# --------------------------------------------------------------------------
# Password recovery (RF-003)
# --------------------------------------------------------------------------
def crear_recuperacion(
    db: Session, *, usuario_id: int, token_hash: str, expira_en: datetime
) -> TokenRecuperacion:
    fila = TokenRecuperacion(usuario_id=usuario_id, token_hash=token_hash, expira_en=expira_en)
    db.add(fila)
    db.flush()
    return fila


def obtener_recuperacion(db: Session, token_hash: str) -> TokenRecuperacion | None:
    consulta = select(TokenRecuperacion).where(TokenRecuperacion.token_hash == token_hash)
    return db.scalars(consulta).first()


def anular_recuperaciones_pendientes(db: Session, usuario_id: int, momento: datetime) -> int:
    """Only the newest reset link works: asking again cancels the previous one."""
    resultado = db.execute(
        update(TokenRecuperacion)
        .where(
            TokenRecuperacion.usuario_id == usuario_id,
            TokenRecuperacion.usado_en.is_(None),
            TokenRecuperacion.expira_en > momento,
        )
        .values(expira_en=momento)
    )
    return int(resultado.rowcount or 0)


# --------------------------------------------------------------------------
# Refresh tokens: the revocation list of RF-005
# --------------------------------------------------------------------------
def registrar_refresco(
    db: Session,
    *,
    jti: str,
    usuario_id: int,
    expira_en: datetime,
    dispositivo: str | None = None,
) -> TokenRefresco:
    fila = TokenRefresco(
        jti=jti, usuario_id=usuario_id, expira_en=expira_en, dispositivo=dispositivo
    )
    db.add(fila)
    db.flush()
    return fila


def obtener_refresco(db: Session, jti: str) -> TokenRefresco | None:
    return db.scalars(select(TokenRefresco).where(TokenRefresco.jti == jti)).first()


def revocar_refresco(db: Session, jti: str, momento: datetime, motivo: str) -> bool:
    """Retire one token. Returns whether this call is the one that did it."""
    resultado = db.execute(
        update(TokenRefresco)
        .where(TokenRefresco.jti == jti, TokenRefresco.revocado_en.is_(None))
        .values(revocado_en=momento, motivo_revocacion=motivo)
    )
    return bool(resultado.rowcount)


def revocar_refrescos_de_usuario(
    db: Session, usuario_id: int, momento: datetime, motivo: str
) -> int:
    """Retire every live token of one account. Returns how many were retired."""
    resultado = db.execute(
        update(TokenRefresco)
        .where(TokenRefresco.usuario_id == usuario_id, TokenRefresco.revocado_en.is_(None))
        .values(revocado_en=momento, motivo_revocacion=motivo)
    )
    return int(resultado.rowcount or 0)


def listar_refrescos_vivos(db: Session, usuario_id: int) -> list[TokenRefresco]:
    consulta = (
        select(TokenRefresco)
        .where(TokenRefresco.usuario_id == usuario_id, TokenRefresco.revocado_en.is_(None))
        .order_by(TokenRefresco.id)
    )
    return list(db.scalars(consulta).all())


# --------------------------------------------------------------------------
# Authentication trail (RNF-014)
# --------------------------------------------------------------------------
def registrar_intento(
    db: Session,
    *,
    correo: str,
    usuario_id: int | None,
    exitoso: bool,
    motivo: str | None = None,
) -> IntentoLogin:
    fila = IntentoLogin(correo=correo, usuario_id=usuario_id, exitoso=exitoso, motivo=motivo)
    db.add(fila)
    db.flush()
    return fila


def listar_intentos(db: Session, correo: str) -> list[IntentoLogin]:
    consulta = (
        select(IntentoLogin)
        .where(IntentoLogin.correo == correo)
        .order_by(IntentoLogin.ocurrido_en, IntentoLogin.id)
    )
    return list(db.scalars(consulta).all())
