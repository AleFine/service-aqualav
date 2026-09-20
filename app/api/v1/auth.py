"""Authentication endpoints (RF-001, RF-002, RF-003, RF-005)."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.deps import get_db, usuario_actual
from app.models import Usuario
from app.schemas import (
    ErrorBody,
    LoginIn,
    LogoutIn,
    MensajeOut,
    RecuperacionIn,
    ReenvioVerificacionIn,
    RefreshIn,
    RegistroIn,
    RestablecerPasswordIn,
    TokenOut,
    UsuarioOut,
    VerificacionIn,
)
from app.services import auth_service
from app.services.auth_service import Sesion
from app.services.ensamblador import armar_usuario

router = APIRouter(prefix="/auth", tags=["autenticación"])

RESPUESTAS = {
    400: {"model": ErrorBody},
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


def _token(sesion: Sesion) -> TokenOut:
    return TokenOut(
        access_token=sesion.access_token,
        refresh_token=sesion.refresh_token,
        expires_in=sesion.expires_in,
        usuario=armar_usuario(sesion.usuario),
        permisos=sesion.permisos,
        verificacion_pendiente=sesion.verificacion_pendiente,
    )


@router.post(
    "/registro",
    response_model=UsuarioOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Crear una cuenta de cliente",
)
def registro(datos: RegistroIn, db: Session = Depends(get_db)) -> UsuarioOut:
    return armar_usuario(auth_service.registrar(db, datos))


@router.post(
    "/login",
    response_model=TokenOut,
    responses=RESPUESTAS,
    summary="Iniciar sesión",
)
def login(datos: LoginIn, db: Session = Depends(get_db)) -> TokenOut:
    return _token(
        auth_service.autenticar(
            db, str(datos.correo), datos.password, dispositivo=datos.dispositivo
        )
    )


@router.post(
    "/refresh",
    response_model=TokenOut,
    responses=RESPUESTAS,
    summary="Renovar los tokens",
)
def refresh(datos: RefreshIn, db: Session = Depends(get_db)) -> TokenOut:
    return _token(auth_service.refrescar(db, datos.refresh_token))


@router.post(
    "/logout",
    response_model=MensajeOut,
    responses=RESPUESTAS,
    summary="Cerrar la sesión de este dispositivo",
)
def logout(datos: LogoutIn, db: Session = Depends(get_db)) -> MensajeOut:
    """RF-005: revokes the refresh token. Reusing it afterwards answers 401.

    Deliberately public: flow 2a has the app queueing the revocation while it
    is offline, and by the time it runs the access token may have expired.
    """
    auth_service.cerrar_sesion(db, datos.refresh_token)
    return MensajeOut(mensaje=auth_service.MENSAJE_SESION_CERRADA)


@router.post(
    "/verificacion",
    response_model=UsuarioOut,
    responses=RESPUESTAS,
    summary="Verificar un correo con el enlace recibido",
)
def verificar(datos: VerificacionIn, db: Session = Depends(get_db)) -> UsuarioOut:
    """RF-001 step 5 and RF-006 flow 3a: activates or applies the new address."""
    return armar_usuario(auth_service.verificar_correo(db, datos.token))


@router.post(
    "/verificacion/reenviar",
    response_model=MensajeOut,
    responses=RESPUESTAS,
    summary="Reenviar el correo de verificación",
)
def reenviar_verificacion(
    datos: ReenvioVerificacionIn, db: Session = Depends(get_db)
) -> MensajeOut:
    """RF-001 flow 5a and RF-002 flow 2c. Same answer for any address."""
    auth_service.reenviar_verificacion(db, str(datos.correo))
    return MensajeOut(mensaje=auth_service.MENSAJE_REENVIO)


@router.post(
    "/password/recuperacion",
    response_model=MensajeOut,
    responses=RESPUESTAS,
    summary="Solicitar el enlace para restablecer la contraseña",
)
def solicitar_recuperacion(datos: RecuperacionIn, db: Session = Depends(get_db)) -> MensajeOut:
    """RF-003 flow 2a: the answer is identical whether the account exists or not."""
    auth_service.solicitar_recuperacion(db, str(datos.correo))
    return MensajeOut(mensaje=auth_service.MENSAJE_RECUPERACION)


@router.post(
    "/password/restablecer",
    response_model=UsuarioOut,
    responses=RESPUESTAS,
    summary="Restablecer la contraseña con el token recibido",
)
def restablecer_password(datos: RestablecerPasswordIn, db: Session = Depends(get_db)) -> UsuarioOut:
    """RF-003 CA-01 y CA-02: single use, thirty minutes, and every session drops."""
    return armar_usuario(auth_service.restablecer_password(db, datos))


@router.get(
    "/yo",
    response_model=UsuarioOut,
    responses=RESPUESTAS,
    summary="Datos del usuario autenticado",
)
def yo(usuario: Usuario = Depends(usuario_actual)) -> UsuarioOut:
    return armar_usuario(usuario)
