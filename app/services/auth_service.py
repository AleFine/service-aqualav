"""Registration, login and token refresh (RF-001, RF-002)."""

import math
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.errors import (
    CorreoYaRegistrado,
    CredencialesInvalidas,
    CuentaBloqueada,
    CuentaDesactivada,
    NoAutenticado,
    detalle,
)
from app.core.horario import ahora_utc, desde_bd
from app.core.security import (
    TIPO_REFRESCO,
    crear_access_token,
    crear_refresh_token,
    decodificar_token,
    hash_password,
    segundos_expiracion_access,
    verify_password,
)
from app.models import EstadoCuenta, Usuario
from app.repositories import usuario as usuario_repo
from app.schemas import RegistroIn
from app.seed import ROL_REGISTRO_PUBLICO
from app.services import eventos

#: RNF-012: five consecutive failures lock the account for fifteen minutes.
INTENTOS_MAXIMOS = 5
BLOQUEO = timedelta(minutes=15)


@dataclass(frozen=True)
class Sesion:
    """Everything ``TokenOut`` needs, built without touching FastAPI."""

    usuario: Usuario
    permisos: list[str]
    access_token: str
    refresh_token: str
    expires_in: int


def _emitir(usuario: Usuario) -> Sesion:
    """Issue a fresh access/refresh pair for an already authenticated user."""
    permisos = usuario.rol.codigos_permisos
    return Sesion(
        usuario=usuario,
        permisos=permisos,
        access_token=crear_access_token(usuario.id, usuario.rol.nombre, permisos),
        refresh_token=crear_refresh_token(usuario.id),
        expires_in=segundos_expiracion_access(),
    )


def _segundos_de_bloqueo(usuario: Usuario) -> int:
    """Seconds left before a locked account accepts logins again, else 0."""
    bloqueado_hasta = desde_bd(usuario.bloqueado_hasta)
    if bloqueado_hasta is None:
        return 0
    restante = (bloqueado_hasta - ahora_utc()).total_seconds()
    return max(0, math.ceil(restante))


def registrar(db: Session, datos: RegistroIn) -> Usuario:
    """Create a customer account (RF-001).

    The password policy and the phone format were already enforced by the
    schema; what is left is the uniqueness of the e-mail (CA-02) and hashing
    the password so it is never stored in clear (CA-03).
    """
    correo = str(datos.correo).strip().lower()
    if usuario_repo.existe_correo(db, correo):
        raise CorreoYaRegistrado(detalles=[detalle("correo", "Ese correo ya tiene una cuenta.")])

    rol = usuario_repo.obtener_rol(db, ROL_REGISTRO_PUBLICO)
    if rol is None:  # pragma: no cover - the seed always creates it
        raise NoAutenticado(
            "No es posible crear cuentas en este momento. Inténtalo más tarde.",
            http_status=503,
        )

    usuario = usuario_repo.crear(
        db,
        nombres=datos.nombres,
        apellidos=datos.apellidos,
        correo=correo,
        telefono=datos.telefono,
        hash_password=hash_password(datos.password),
        rol_id=rol.id,
        estado_cuenta=EstadoCuenta.ACTIVA.value,
    )

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario.id,
        eventos.USUARIO_REGISTRADO,
        autor_id=usuario.id,
        datos={"correo": correo},
    )
    db.commit()
    db.refresh(usuario)
    return usuario


def autenticar(db: Session, correo: str, password: str) -> Sesion:
    """Validate credentials and issue the token pair (RF-002).

    Failed attempts are counted on the account; the fifth one locks it for
    fifteen minutes and the API answers 429 with the remaining seconds
    (CA-02). A successful login clears the counter. An account the
    administrator deactivated is refused with 403 (RF-035 CA-01).
    """
    usuario = usuario_repo.obtener_por_correo(db, correo)

    if usuario is None:
        # Same generic error as a wrong password: never confirm that an
        # address exists (it would allow enumerating accounts).
        raise CredencialesInvalidas()

    restante = _segundos_de_bloqueo(usuario)
    if restante > 0:
        raise CuentaBloqueada(
            detalles=[
                detalle(
                    "bloqueado_hasta",
                    f"Vuelve a intentarlo en {math.ceil(restante / 60)} minuto(s).",
                ),
                detalle("segundos_restantes", str(restante)),
            ]
        )

    if not verify_password(password, usuario.hash_password):
        usuario.intentos_fallidos = (usuario.intentos_fallidos or 0) + 1
        if usuario.intentos_fallidos >= INTENTOS_MAXIMOS:
            usuario.bloqueado_hasta = ahora_utc() + BLOQUEO
            usuario.intentos_fallidos = 0
            db.commit()
            segundos = int(BLOQUEO.total_seconds())
            raise CuentaBloqueada(
                detalles=[
                    detalle(
                        "bloqueado_hasta",
                        f"Vuelve a intentarlo en {int(segundos / 60)} minuto(s).",
                    ),
                    detalle("segundos_restantes", str(segundos)),
                ]
            )
        db.commit()
        raise CredencialesInvalidas()

    if usuario.estado_cuenta != EstadoCuenta.ACTIVA.value:
        # RF-035 CA-01: a deactivated worker gets 403, not 401. The password was
        # right; what is missing is the authorization to use the account, and
        # saying so is what sends them to the administrator instead of to the
        # "forgot my password" screen.
        raise CuentaDesactivada(
            detalles=[detalle("correo", "La cuenta está desactivada por el administrador.")]
        )

    usuario.intentos_fallidos = 0
    usuario.bloqueado_hasta = None
    db.commit()
    db.refresh(usuario)
    return _emitir(usuario)


def revocar_tokens_de_refresco(db: Session, usuario_id: int, motivo: str) -> None:
    """Invalidate every refresh token of a user (RF-004 flow 4a).

    HOOK, on purpose. The revocation list lives in ``token_refresco.jti``, and
    that table arrives with RF-005 in INC-3: today a refresh token is a
    self-contained JWT and there is nowhere to write the revocation, so all the
    hook can do is leave the intent in the event log (P7), with the moment and
    the reason, for the audit trail of RF-036.

    TODO(INC-3, RF-005): replace the body with the real revocation - insert the
    ``jti`` of every live refresh token of ``usuario_id`` in the revocation
    list and make :func:`refrescar` reject the ones listed. The call sites
    (:mod:`app.services.rol_service`) must not change.

    The caller owns the transaction: nothing is committed here.
    """
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario_id,
        eventos.USUARIO_TOKENS_REVOCADOS,
        autor_id=None,
        datos={"motivo": motivo, "ocurrido_en": ahora_utc().isoformat()},
    )


def refrescar(db: Session, refresh_token: str) -> Sesion:
    """Exchange a valid refresh token for a brand new pair (RF-002).

    An access token presented here is rejected: ``decodificar_token`` checks
    the ``type`` claim against ``TIPO_REFRESCO``.
    """
    payload = decodificar_token(refresh_token, TIPO_REFRESCO)
    if payload is None:
        raise NoAutenticado()

    usuario = usuario_repo.obtener_por_id(db, payload.usuario_id)
    if usuario is None or usuario.estado_cuenta != EstadoCuenta.ACTIVA.value:
        raise NoAutenticado()

    return _emitir(usuario)
