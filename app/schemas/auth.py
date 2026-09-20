"""Authentication payloads (RF-001, RF-002)."""

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.password import validar_politica
from app.schemas.common import MENSAJE_TELEFONO, TELEFONO_REGEX, normalizar_telefono
from app.schemas.usuario import UsuarioOut

__all__ = [
    "MENSAJE_TELEFONO",
    "TELEFONO_REGEX",
    "LoginIn",
    "RefreshIn",
    "RegistroIn",
    "TokenOut",
    "normalizar_telefono",
    "validar_password",
]


def validar_password(valor: str) -> str:
    """Enforce the password policy with a Spanish message (RNF-009 M1/M3)."""
    faltantes = validar_politica(valor)
    if faltantes:
        raise ValueError("La contraseña no cumple los requisitos: " + " ".join(faltantes))
    return valor


class RegistroIn(BaseModel):
    nombres: str = Field(min_length=1, max_length=80)
    apellidos: str = Field(min_length=1, max_length=80)
    correo: EmailStr
    telefono: str = Field(max_length=20)
    password: str = Field(max_length=128)
    acepta_politica: bool

    @field_validator("nombres", "apellidos")
    @classmethod
    def _limpiar(cls, valor: str) -> str:
        return valor.strip()

    @field_validator("telefono")
    @classmethod
    def _telefono(cls, valor: str) -> str:
        return normalizar_telefono(valor)

    @field_validator("password")
    @classmethod
    def _password(cls, valor: str) -> str:
        return validar_password(valor)

    @field_validator("acepta_politica")
    @classmethod
    def _politica(cls, valor: bool) -> bool:
        if not valor:
            raise ValueError("Debes aceptar la política de tratamiento de datos para continuar.")
        return valor


class LoginIn(BaseModel):
    """Login is JSON, not an OAuth2 form."""

    correo: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    usuario: UsuarioOut
    permisos: list[str] = Field(default_factory=list)
