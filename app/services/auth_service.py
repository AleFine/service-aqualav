"""Registration, login, sessions and credentials (RF-001..RF-003, RF-005).

The MVP could only promise three of these on paper, because a refresh token was
a self-contained JWT and there was nowhere to write down that one had been
retired. ``app.models.autenticacion`` is that place, and it turns the three
promises into state the server checks:

* RF-001 leaves a self-registered account in ``pendiente_verificacion`` and
  mails a link. Flow 5a: if the mail cannot be delivered the account is created
  anyway and the customer may ask for another link;
* RF-003 mails a single-use token that lives thirty minutes, answers exactly
  the same whether the address exists or not (flow 2a), and drops every session
  of the account when the password actually changes;
* RF-005 revokes a refresh token by its ``jti``, which is what makes
  :func:`revocar_tokens_de_refresco` - the hook INC-1A left behind - real.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import (
    CorreoYaRegistrado,
    CredencialesInvalidas,
    CuentaBloqueada,
    CuentaDesactivada,
    DocumentoYaRegistrado,
    NoAutenticado,
    TokenInvalido,
    detalle,
)
from app.core.horario import ahora_utc, desde_bd
from app.core.security import (
    TIPO_REFRESCO,
    crear_access_token,
    crear_refresh_token,
    decodificar_token,
    expiracion_refresh,
    generar_jti,
    generar_token_enlace,
    hash_password,
    hash_token,
    segundos_expiracion_access,
    verify_password,
)
from app.models import ESTADOS_CUENTA_CON_ACCESO, EstadoCuenta, Usuario
from app.repositories import autenticacion as autenticacion_repo
from app.repositories import usuario as usuario_repo
from app.schemas import RegistroIn, RestablecerPasswordIn
from app.seed import ROL_REGISTRO_PUBLICO
from app.services import eventos
from app.services.proveedores.correo import (
    PROVEEDOR_CORREO_PREDETERMINADO,
    EnvioDeCorreoFallido,
    ProveedorCorreo,
)

#: RNF-012: five consecutive failures lock the account for fifteen minutes.
INTENTOS_MAXIMOS = 5
BLOQUEO = timedelta(minutes=15)

ASUNTO_VERIFICACION = "Verifica tu correo en AquaLav"
ASUNTO_CORREO_NUEVO = "Confirma tu nuevo correo en AquaLav"
ASUNTO_RECUPERACION = "Recupera tu contraseña de AquaLav"

#: RF-003 flow 2a: ONE message for every outcome of "I forgot my password".
#: Whether the address has an account, whether the account is suspended and
#: whether the mail actually left are all invisible from here - any of them
#: would let a stranger find out who is registered.
MENSAJE_RECUPERACION = (
    "Si el correo corresponde a una cuenta, te enviamos un enlace para "
    "restablecer la contraseña. Revisa tu bandeja de entrada."
)
MENSAJE_REENVIO = (
    "Si el correo corresponde a una cuenta pendiente de verificación, "
    "te enviamos el enlace nuevamente. Revisa tu bandeja de entrada."
)
MENSAJE_SESION_CERRADA = "Cerraste la sesión en este dispositivo."


@dataclass(frozen=True)
class Sesion:
    """Everything ``TokenOut`` needs, built without touching FastAPI."""

    usuario: Usuario
    permisos: list[str]
    access_token: str
    refresh_token: str
    expires_in: int
    #: RF-002 flow 2c: the session is valid and the address is still
    #: unconfirmed, so the client can offer to send the link again.
    verificacion_pendiente: bool = False


# --------------------------------------------------------------------------
# Mail bodies (the provider is simulated; plan section 4)
# --------------------------------------------------------------------------
def _enlace(ruta: str, token: str) -> str:
    return f"{settings.url_base_app}/{ruta}?token={token}"


def _cuerpo_verificacion(usuario: Usuario, correo: str, token: str) -> str:
    return (
        f"Hola {usuario.nombres}:\n\n"
        f"Confirma que {correo} es tu correo abriendo este enlace:\n"
        f"{_enlace('verificar-correo', token)}\n\n"
        f"El enlace vence en {settings.verificacion_correo_expira_horas} horas."
    )


def _cuerpo_recuperacion(usuario: Usuario, token: str) -> str:
    return (
        f"Hola {usuario.nombres}:\n\n"
        "Pediste restablecer tu contraseña de AquaLav. Usa este enlace:\n"
        f"{_enlace('restablecer-password', token)}\n\n"
        f"Vence en {settings.recuperacion_expira_minutos} minutos y solo puede usarse una vez. "
        "Si no fuiste tú, ignora este mensaje: tu contraseña no cambió."
    )


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
def _emitir(db: Session, usuario: Usuario, dispositivo: str | None = None) -> Sesion:
    """Issue a token pair and write the refresh token down (RF-005).

    The row in ``token_refresco`` is what makes the pair revocable: from here
    on a refresh token is usable only while its ``jti`` has a live row. The
    caller owns the transaction; nothing is committed here.
    """
    permisos = usuario.rol.codigos_permisos
    jti = generar_jti()
    autenticacion_repo.registrar_refresco(
        db,
        jti=jti,
        usuario_id=usuario.id,
        expira_en=expiracion_refresh(),
        dispositivo=dispositivo,
    )
    return Sesion(
        usuario=usuario,
        permisos=permisos,
        access_token=crear_access_token(usuario.id, usuario.rol.nombre, permisos),
        refresh_token=crear_refresh_token(usuario.id, jti),
        expires_in=segundos_expiracion_access(),
        verificacion_pendiente=(usuario.estado_cuenta == EstadoCuenta.PENDIENTE_VERIFICACION.value),
    )


def _segundos_de_bloqueo(usuario: Usuario) -> int:
    """Seconds left before a locked account accepts logins again, else 0."""
    bloqueado_hasta = desde_bd(usuario.bloqueado_hasta)
    if bloqueado_hasta is None:
        return 0
    restante = (bloqueado_hasta - ahora_utc()).total_seconds()
    return max(0, math.ceil(restante))


def _anotar_intento(
    db: Session, correo: str, usuario: Usuario | None, *, exitoso: bool, motivo: str | None = None
) -> None:
    """Append one row to the authentication trail (RNF-014, RF-036)."""
    autenticacion_repo.registrar_intento(
        db,
        correo=correo,
        usuario_id=usuario.id if usuario else None,
        exitoso=exitoso,
        motivo=motivo,
    )


# --------------------------------------------------------------------------
# E-mail verification (RF-001, RF-002 flow 2c, RF-006 flow 3a)
# --------------------------------------------------------------------------
def _abrir_verificacion(db: Session, usuario: Usuario, correo: str) -> str:
    """Retire any pending link and open a new one. Returns the plain token."""
    momento = ahora_utc()
    autenticacion_repo.anular_verificaciones_pendientes(db, usuario.id, momento)
    token = generar_token_enlace()
    autenticacion_repo.crear_verificacion(
        db,
        usuario_id=usuario.id,
        correo=correo,
        token_hash=hash_token(token),
        expira_en=momento + timedelta(hours=settings.verificacion_correo_expira_horas),
    )
    return token


def enviar_verificacion(
    db: Session,
    usuario: Usuario,
    correo: str,
    proveedor: ProveedorCorreo = PROVEEDOR_CORREO_PREDETERMINADO,
    *,
    asunto: str = ASUNTO_VERIFICACION,
) -> bool:
    """Open a verification of ``correo`` and try to deliver it. Never raises.

    Public because RF-006 reaches the very same act from the profile: asking
    to change an address is opening a verification for the NEW one, and the
    change only lands when that link is used (CA-02).

    RF-001 flow 5a: a delivery that fails is recorded and swallowed, because
    the account has to exist either way; the customer asks for another link
    with ``POST /auth/verificacion/reenviar``.
    """
    token = _abrir_verificacion(db, usuario, correo)
    entregada = True
    try:
        proveedor.enviar(correo, asunto, _cuerpo_verificacion(usuario, correo, token))
    except EnvioDeCorreoFallido:
        entregada = False

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario.id,
        (
            eventos.USUARIO_VERIFICACION_ENVIADA
            if entregada
            else eventos.USUARIO_VERIFICACION_NO_ENVIADA
        ),
        autor_id=usuario.id,
        datos={"correo": correo},
    )
    return entregada


def _token_vigente(expira_en: datetime | None, consumido_en: datetime | None) -> bool:
    """A link token is usable while it is neither spent nor expired."""
    if consumido_en is not None:
        return False
    vence = desde_bd(expira_en)
    return vence is not None and vence > ahora_utc()


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def registrar(
    db: Session,
    datos: RegistroIn,
    *,
    correo_proveedor: ProveedorCorreo = PROVEEDOR_CORREO_PREDETERMINADO,
) -> Usuario:
    """Create a customer account (RF-001).

    The password policy, the phone and the document format were already
    enforced by the schema; what is left is the two uniqueness rules - the
    e-mail (CA-02) and, new in v1.0, the document number - hashing the password
    so it is never stored in clear (CA-03), and mailing the verification link
    that leaves the account in ``pendiente_verificacion``.
    """
    correo = str(datos.correo).strip().lower()
    if usuario_repo.existe_correo(db, correo):
        raise CorreoYaRegistrado(detalles=[detalle("correo", "Ese correo ya tiene una cuenta.")])

    if usuario_repo.existe_documento(db, datos.numero_documento):
        raise DocumentoYaRegistrado(
            detalles=[detalle("numero_documento", "Ese documento ya tiene una cuenta.")]
        )

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
        # RF-001 postcondition v1.0: the account is born unverified.
        estado_cuenta=EstadoCuenta.PENDIENTE_VERIFICACION.value,
        tipo_documento=datos.tipo_documento.value,
        numero_documento=datos.numero_documento,
        # RNF-018: the schema refuses a registration without the tick; this is
        # the receipt of when it was given.
        consentimiento_privacidad_en=ahora_utc(),
    )

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario.id,
        eventos.USUARIO_REGISTRADO,
        autor_id=usuario.id,
        datos={"correo": correo, "tipo_documento": datos.tipo_documento.value},
    )
    enviar_verificacion(db, usuario, correo, correo_proveedor)
    db.commit()
    db.refresh(usuario)
    return usuario


def verificar_correo(db: Session, token: str) -> Usuario:
    """Consume a verification link (RF-001 step 5, RF-006 flow 3a).

    Two outcomes share one entry point because they are the same act: the link
    that confirms the address an account was born with ACTIVATES it, and the
    link that confirms a new address APPLIES the change RF-006 CA-02 refused to
    apply until now.
    """
    fila = autenticacion_repo.obtener_verificacion(db, hash_token(token))
    if fila is None or not _token_vigente(fila.expira_en, fila.verificado_en):
        raise TokenInvalido(detalles=[detalle("token", "Solicita un enlace nuevo.")])

    usuario = fila.usuario
    momento = ahora_utc()
    fila.verificado_en = momento

    if fila.correo != usuario.correo:
        # The address may have been taken by somebody else while the mail sat
        # in the inbox; the uniqueness of ``usuario.correo`` decides, not the
        # order in which the links were opened.
        if usuario_repo.existe_correo(db, fila.correo):
            raise CorreoYaRegistrado(
                detalles=[detalle("correo", "Ese correo ya tiene una cuenta.")]
            )
        anterior = usuario.correo
        usuario.correo = fila.correo
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_USUARIO,
            usuario.id,
            eventos.USUARIO_CORREO_CAMBIADO,
            autor_id=usuario.id,
            datos={"anterior": anterior, "nuevo": fila.correo},
        )

    if usuario.estado_cuenta == EstadoCuenta.PENDIENTE_VERIFICACION.value:
        usuario.estado_cuenta = EstadoCuenta.ACTIVA.value

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario.id,
        eventos.USUARIO_CORREO_VERIFICADO,
        autor_id=usuario.id,
        datos={"correo": fila.correo},
    )
    db.commit()
    db.refresh(usuario)
    return usuario


def reenviar_verificacion(
    db: Session,
    correo: str,
    *,
    correo_proveedor: ProveedorCorreo = PROVEEDOR_CORREO_PREDETERMINADO,
) -> None:
    """Send the verification link again (RF-001 flow 5a, RF-002 flow 2c).

    Answers the same for an address with no account, an account that is
    already verified and one that is not: the caller never learns which.
    """
    destino = (correo or "").strip().lower()
    usuario = usuario_repo.obtener_por_correo(db, destino)
    if usuario is not None and usuario.estado_cuenta == EstadoCuenta.PENDIENTE_VERIFICACION.value:
        enviar_verificacion(db, usuario, usuario.correo, correo_proveedor)
        db.commit()


def autenticar(
    db: Session, correo: str, password: str, *, dispositivo: str | None = None
) -> Sesion:
    """Validate credentials and issue the token pair (RF-002).

    Failed attempts are counted on the account; the fifth one locks it for
    fifteen minutes and the API answers 429 with the remaining seconds
    (CA-02). A successful login clears the counter. An account the
    administrator deactivated is refused with 403 (RF-035 CA-01); one that has
    not verified its address is NOT - see ``ESTADOS_CUENTA_CON_ACCESO`` - and
    the session carries ``verificacion_pendiente`` so the client can offer the
    resend of flow 2c. Every attempt, successful or not, is written to the
    authentication trail (RNF-014).
    """
    buscado = (correo or "").strip().lower()
    usuario = usuario_repo.obtener_por_correo(db, buscado)

    if usuario is None:
        # Same generic error as a wrong password: never confirm that an
        # address exists (it would allow enumerating accounts).
        _anotar_intento(db, buscado, None, exitoso=False, motivo=CredencialesInvalidas.codigo)
        db.commit()
        raise CredencialesInvalidas()

    restante = _segundos_de_bloqueo(usuario)
    if restante > 0:
        _anotar_intento(db, buscado, usuario, exitoso=False, motivo=CuentaBloqueada.codigo)
        db.commit()
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
            _anotar_intento(db, buscado, usuario, exitoso=False, motivo=CuentaBloqueada.codigo)
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
        _anotar_intento(db, buscado, usuario, exitoso=False, motivo=CredencialesInvalidas.codigo)
        db.commit()
        raise CredencialesInvalidas()

    if usuario.estado_cuenta not in ESTADOS_CUENTA_CON_ACCESO:
        # RF-035 CA-01: a deactivated worker gets 403, not 401. The password was
        # right; what is missing is the authorization to use the account, and
        # saying so is what sends them to the administrator instead of to the
        # "forgot my password" screen.
        _anotar_intento(db, buscado, usuario, exitoso=False, motivo=CuentaDesactivada.codigo)
        db.commit()
        raise CuentaDesactivada(
            detalles=[detalle("correo", "La cuenta está desactivada por el administrador.")]
        )

    usuario.intentos_fallidos = 0
    usuario.bloqueado_hasta = None
    _anotar_intento(db, buscado, usuario, exitoso=True)
    sesion = _emitir(db, usuario, dispositivo)
    db.commit()
    db.refresh(usuario)
    return sesion


def revocar_tokens_de_refresco(db: Session, usuario_id: int, motivo: str) -> int:
    """Invalidate every refresh token of a user (RF-004 flow 4a, RF-003).

    INC-1A left this as a hook that could only write down the intention,
    because there was no revocation list to write to. There is one now: every
    live row of ``token_refresco`` is stamped, so the next ``POST
    /auth/refresh`` with any of those tokens answers 401 (RF-005 CA-01).

    The call sites did not change: the role change of RF-004 and the password
    reset of RF-003 both reach the revocation through here. Returns how many
    sessions were dropped. The caller owns the transaction: nothing is
    committed here.
    """
    momento = ahora_utc()
    revocados = autenticacion_repo.revocar_refrescos_de_usuario(db, usuario_id, momento, motivo)
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario_id,
        eventos.USUARIO_TOKENS_REVOCADOS,
        autor_id=None,
        datos={
            "motivo": motivo,
            "ocurrido_en": momento.isoformat(),
            "sesiones_revocadas": revocados,
        },
    )
    return revocados


def refrescar(db: Session, refresh_token: str, *, dispositivo: str | None = None) -> Sesion:
    """Exchange a valid refresh token for a brand new pair (RF-002, RF-005).

    Three things can make a refresh token unusable, and all three answer 401:
    it is not a refresh token at all (``decodificar_token`` checks the ``type``
    claim), its ``jti`` is not in the revocation list, or its row is stamped -
    which is what signing out (CA-01), a role change or a password reset do.

    A token with no ``jti`` claim is one issued before the list existed; it is
    refused as well, because an unrevocable session is exactly what RF-005 is
    there to end.
    """
    payload = decodificar_token(refresh_token, TIPO_REFRESCO)
    if payload is None or not payload.jti:
        raise NoAutenticado()

    fila = autenticacion_repo.obtener_refresco(db, payload.jti)
    if fila is None or fila.revocado_en is not None:
        raise NoAutenticado()

    usuario = usuario_repo.obtener_por_id(db, payload.usuario_id)
    if usuario is None or usuario.estado_cuenta not in ESTADOS_CUENTA_CON_ACCESO:
        raise NoAutenticado()

    sesion = _emitir(db, usuario, dispositivo or fila.dispositivo)
    db.commit()
    return sesion


def cerrar_sesion(db: Session, refresh_token: str) -> None:
    """Retire one refresh token (RF-005 CA-01).

    Idempotent and silent: an unknown, malformed or already retired token
    answers exactly like a successful sign-out. The client is throwing the
    credential away regardless, and an error here would only tell a stranger
    whether the token they hold is live.
    """
    payload = decodificar_token(refresh_token, TIPO_REFRESCO)
    if payload is None or not payload.jti:
        return

    if autenticacion_repo.revocar_refresco(
        db, payload.jti, ahora_utc(), eventos.USUARIO_SESION_CERRADA
    ):
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_USUARIO,
            payload.usuario_id,
            eventos.USUARIO_SESION_CERRADA,
            autor_id=payload.usuario_id,
            datos={"jti": payload.jti},
        )
        db.commit()


def solicitar_recuperacion(
    db: Session,
    correo: str,
    *,
    correo_proveedor: ProveedorCorreo = PROVEEDOR_CORREO_PREDETERMINADO,
) -> None:
    """Start a password reset (RF-003 steps 1 and 2).

    Flow 2a is the whole point: this function returns ``None`` no matter what
    happened, so the router can answer the exact same body for an address with
    an account and for one without. The token lives thirty minutes and asking
    again retires the previous one.
    """
    destino = (correo or "").strip().lower()
    usuario = usuario_repo.obtener_por_correo(db, destino)
    if usuario is None or usuario.estado_cuenta not in ESTADOS_CUENTA_CON_ACCESO:
        return

    momento = ahora_utc()
    autenticacion_repo.anular_recuperaciones_pendientes(db, usuario.id, momento)
    token = generar_token_enlace()
    autenticacion_repo.crear_recuperacion(
        db,
        usuario_id=usuario.id,
        token_hash=hash_token(token),
        expira_en=momento + timedelta(minutes=settings.recuperacion_expira_minutos),
    )

    try:
        correo_proveedor.enviar(destino, ASUNTO_RECUPERACION, _cuerpo_recuperacion(usuario, token))
    except EnvioDeCorreoFallido:
        # Same shape as RF-001 flow 5a: the request is recorded, the delivery
        # is not guaranteed, and the person asks again.
        pass

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario.id,
        eventos.USUARIO_RECUPERACION_SOLICITADA,
        autor_id=usuario.id,
        datos={"correo": destino},
    )
    db.commit()


def restablecer_password(db: Session, datos: RestablecerPasswordIn) -> Usuario:
    """Spend a reset token and change the password (RF-003 CA-01, CA-02).

    An expired token, one already spent and one that never existed are the
    same 400: telling them apart would say whether a reset is in flight for
    an address. Changing the password INVALIDATES EVERY PREVIOUS SESSION,
    which is the last sentence of the requirement and the reason
    :func:`revocar_tokens_de_refresco` had to become real.
    """
    fila = autenticacion_repo.obtener_recuperacion(db, hash_token(datos.token))
    if fila is None or not _token_vigente(fila.expira_en, fila.usado_en):
        raise TokenInvalido(detalles=[detalle("token", "Solicita un enlace nuevo.")])

    momento = ahora_utc()
    fila.usado_en = momento

    usuario = fila.usuario
    usuario.hash_password = hash_password(datos.password)
    # Somebody who proved they own the address should not stay locked out by
    # the failed attempts that sent them here (RNF-012).
    usuario.intentos_fallidos = 0
    usuario.bloqueado_hasta = None

    autenticacion_repo.anular_recuperaciones_pendientes(db, usuario.id, momento)
    revocar_tokens_de_refresco(db, usuario.id, motivo=eventos.USUARIO_PASSWORD_RESTABLECIDA)

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario.id,
        eventos.USUARIO_PASSWORD_RESTABLECIDA,
        autor_id=usuario.id,
        datos={"correo": usuario.correo},
    )
    db.commit()
    db.refresh(usuario)
    return usuario
