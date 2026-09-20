"""Password hashing and JWT issuing / decoding (RNF-012, contract section 4)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

# bcrypt with cost 12 as mandated by RNF-012.
contexto_password = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

TIPO_ACCESO = "access"
TIPO_REFRESCO = "refresh"


@dataclass(frozen=True)
class PayloadToken:
    """Decoded and already validated JWT payload."""

    usuario_id: int
    tipo: str
    rol: str | None = None
    permisos: tuple[str, ...] = field(default_factory=tuple)
    expira_en: datetime | None = None


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


def crear_refresh_token(usuario_id: int) -> str:
    """Issue a 7 day refresh token. It carries no role nor permissions."""
    return _codificar(
        {"sub": str(usuario_id), "type": TIPO_REFRESCO},
        timedelta(days=settings.refresh_token_expire_days),
    )


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

    return PayloadToken(
        usuario_id=usuario_id,
        tipo=cuerpo["type"],
        rol=cuerpo.get("rol"),
        permisos=tuple(str(codigo) for codigo in permisos),
        expira_en=expira_en,
    )
