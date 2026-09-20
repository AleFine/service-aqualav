"""Authentication endpoints (RF-001, RF-002)."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.deps import get_db, usuario_actual
from app.models import Usuario
from app.schemas import ErrorBody, LoginIn, RefreshIn, RegistroIn, TokenOut, UsuarioOut
from app.services import auth_service
from app.services.auth_service import Sesion
from app.services.ensamblador import armar_usuario

router = APIRouter(prefix="/auth", tags=["autenticación"])

RESPUESTAS = {400: {"model": ErrorBody}, 401: {"model": ErrorBody}, 409: {"model": ErrorBody}}


def _token(sesion: Sesion) -> TokenOut:
    return TokenOut(
        access_token=sesion.access_token,
        refresh_token=sesion.refresh_token,
        expires_in=sesion.expires_in,
        usuario=armar_usuario(sesion.usuario),
        permisos=sesion.permisos,
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
    return _token(auth_service.autenticar(db, str(datos.correo), datos.password))


@router.post(
    "/refresh",
    response_model=TokenOut,
    responses=RESPUESTAS,
    summary="Renovar los tokens",
)
def refresh(datos: RefreshIn, db: Session = Depends(get_db)) -> TokenOut:
    return _token(auth_service.refrescar(db, datos.refresh_token))


@router.get(
    "/yo",
    response_model=UsuarioOut,
    responses=RESPUESTAS,
    summary="Datos del usuario autenticado",
)
def yo(usuario: Usuario = Depends(usuario_actual)) -> UsuarioOut:
    return armar_usuario(usuario)
