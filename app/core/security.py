"""Password hashing and JWT issuing / decoding (RNF-012, contract section 4)."""

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

# bcrypt with cost 12 as mandated by RNF-012.
contexto_password = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

TIPO_ACCESO = "access"
TIPO_REFRESCO = "refresh"

#: Bytes of entropy behind a link token (RF-001 verification, RF-003 reset).
#: ``token_urlsafe`` yields roughly 4/3 characters per byte, so this is a 43
#: character token that survives being pasted out of an e-mail client.
BYTES_TOKEN_ENLACE = 32


@dataclass(frozen=True)
class PayloadToken:
    """Decoded and already validated JWT payload."""

    usuario_id: int
    tipo: str
    rol: str | None = None
    permisos: tuple[str, ...] = field(default_factory=tuple)
    expira_en: datetime | None = None
    #: Identifier of a refresh token, the key of the revocation list (RF-005).
    #: Access tokens do not carry one: they are short lived by design and are
    #: never revoked one by one.
    jti: str | None = None


def hash_password(password: str) -> str:
    """Return the bcrypt hash of a plain password."""
    return contexto_password.hash(password)


def verify_password(password: str, hash_guardado: str) -> bool:
    """Check a plain password against a stored bcrypt hash."""
    try:
        return contexto_password.verify(password, hash_guardado)
    except ValueError:
        # Malformed / truncated hash in the database: treat as a failed login.
        return False


def segundos_expiracion_access() -> int:
    """Lifetime of an access token in seconds (the API's ``expires_in``)."""
    return settings.access_token_expire_minutes * 60


def _codificar(datos: dict, expira: timedelta) -> str:
    ahora_utc = datetime.now(UTC)
    cuerpo = dict(datos)
    cuerpo["iat"] = int(ahora_utc.timestamp())
    cuerpo["exp"] = int((ahora_utc + expira).timestamp())
    return jwt.encode(cuerpo, settings.secret_key, algorithm=settings.algorithm)


def crear_access_token(usuario_id: int, rol: str, permisos: list[str]) -> str:
    """Issue a 30 minute access token carrying the caller's role and permissions."""
    return _codificar(
        {
            "sub": str(usuario_id),
            "rol": rol,
            "permisos": list(permisos),
            "type": TIPO_ACCESO,
        },
        timedelta(minutes=settings.access_token_expire_minutes),
    )


def generar_jti() -> str:
    """A fresh identifier for a refresh token (RF-005)."""
    return secrets.token_hex(16)


def crear_refresh_token(usuario_id: int, jti: str) -> str:
    """Issue a 7 day refresh token. It carries no role nor permissions.

    ``jti`` is what makes the token revocable: the row in ``token_refresco``
    is looked up by it on every refresh, so signing out or changing a role can
    retire a token that has not expired yet.
    """
    return _codificar(
        {"sub": str(usuario_id), "type": TIPO_REFRESCO, "jti": jti},
        timedelta(days=settings.refresh_token_expire_days),
    )


def expiracion_refresh() -> datetime:
    """When a refresh token issued right now stops being valid."""
    return datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)


def generar_token_enlace() -> str:
    """A random token for a verification or password-reset link."""
    return secrets.token_urlsafe(BYTES_TOKEN_ENLACE)


def hash_token(token: str) -> str:
    """Fingerprint of a link token: what the database stores, never the token.

    SHA-256 and not bcrypt on purpose. A bcrypt hash exists to survive a
    dictionary attack over something a human chose; these tokens are 256 bits
    of ``secrets`` output, so there is no dictionary, and the lookup happens on
    every click of the link.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def decodificar_token(token: str, tipo_esperado: str) -> PayloadToken | None:
    """Decode a JWT and return its payload, or ``None`` when it is not usable.

    Returns ``None`` when the signature is invalid, the token expired, the
    subject is missing/not numeric, or the ``type`` claim does not match
    ``tipo_esperado`` (an access token presented where a refresh token is
    expected is rejected, and vice versa).
    """
    try:
        cuerpo = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        return None

    if cuerpo.get("type") != tipo_esperado:
        return None

    sub = cuerpo.get("sub")
    try:
        usuario_id = int(sub)
    except (TypeError, ValueError):
        return None

    exp = cuerpo.get("exp")
    expira_en = datetime.fromtimestamp(exp, tz=UTC) if exp else None

    permisos = cuerpo.get("permisos") or []
    if not isinstance(permisos, list):
        return None

    jti = cuerpo.get("jti")

    return PayloadToken(
        usuario_id=usuario_id,
        tipo=cuerpo["type"],
        rol=cuerpo.get("rol"),
        permisos=tuple(str(codigo) for codigo in permisos),
        expira_en=expira_en,
        jti=str(jti) if jti else None,
    )
