"""Authentication payloads (RF-001, RF-002)."""

import re

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.password import validar_politica
from app.schemas.usuario import UsuarioOut

# Peruvian mobile number: 9 digits starting with 9.
TELEFONO_REGEX = re.compile(r"^9\d{8}$")
MENSAJE_TELEFONO = "El teléfono debe tener 9 dígitos y empezar con 9."


def normalizar_telefono(valor: str) -> str:
    """Strip spaces, dashes and the +51 prefix, then validate the format."""
    limpio = re.sub(r"[\s\-()]", "", valor or "")
    if limpio.startswith("+51"):
        limpio = limpio[3:]
    if not TELEFONO_REGEX.match(limpio):
        raise ValueError(MENSAJE_TELEFONO)
    return limpio


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
