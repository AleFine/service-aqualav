"""Authentication payloads (RF-001, RF-002, RF-003, RF-005)."""

import re

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.core.password import validar_politica
from app.models.enums import TipoDocumento
from app.schemas.common import MENSAJE_TELEFONO, TELEFONO_REGEX, normalizar_telefono
from app.schemas.usuario import UsuarioOut

__all__ = [
    "MENSAJE_DOCUMENTO",
    "MENSAJE_TELEFONO",
    "TELEFONO_REGEX",
    "LoginIn",
    "LogoutIn",
    "MensajeOut",
    "RecuperacionIn",
    "RefreshIn",
    "RegistroIn",
    "ReenvioVerificacionIn",
    "RestablecerPasswordIn",
    "TokenOut",
    "VerificacionIn",
    "normalizar_documento",
    "normalizar_telefono",
    "validar_password",
]

#: Peruvian DNI is exactly eight digits; the other two documents are alphanumeric
#: and their length varies by issuing country, so only a range is enforced.
DOCUMENTO_REGEX = re.compile(r"^[A-Z0-9]{6,20}$")
LONGITUD_DNI = 8
MENSAJE_DOCUMENTO = "El documento debe tener entre 6 y 20 caracteres alfanuméricos."
MENSAJE_DNI = f"El DNI debe tener exactamente {LONGITUD_DNI} dígitos."


def validar_password(valor: str) -> str:
    """Enforce the password policy with a Spanish message (RNF-009 M1/M3)."""
    faltantes = validar_politica(valor)
    if faltantes:
        raise ValueError("La contraseña no cumple los requisitos: " + " ".join(faltantes))
    return valor


def normalizar_documento(valor: str) -> str:
    """Uppercase and blank-free, the form the uniqueness key is built on."""
    limpio = "".join((valor or "").split()).upper()
    if not DOCUMENTO_REGEX.match(limpio):
        raise ValueError(MENSAJE_DOCUMENTO)
    return limpio


class RegistroIn(BaseModel):
    """RF-001 v1.0: the form now also captures the identity document."""

    nombres: str = Field(min_length=1, max_length=80)
    apellidos: str = Field(min_length=1, max_length=80)
    correo: EmailStr
    telefono: str = Field(max_length=20)
    tipo_documento: TipoDocumento
    numero_documento: str = Field(max_length=20)
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

    @field_validator("numero_documento")
    @classmethod
    def _documento(cls, valor: str) -> str:
        return normalizar_documento(valor)

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

    @model_validator(mode="after")
    def _dni_de_ocho_digitos(self) -> "RegistroIn":
        """A DNI has a shape of its own; the other documents do not."""
        if self.tipo_documento is TipoDocumento.DNI and not (
            self.numero_documento.isdigit() and len(self.numero_documento) == LONGITUD_DNI
        ):
            raise ValueError(MENSAJE_DNI)
        return self


class LoginIn(BaseModel):
    """Login is JSON, not an OAuth2 form."""

    correo: EmailStr
    password: str = Field(min_length=1, max_length=128)
    #: RF-005: free text so the person can tell their open sessions apart.
    dispositivo: str | None = Field(default=None, max_length=120)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=1)


class LogoutIn(BaseModel):
    """RF-005: the token to retire travels in the body, not in the header.

    Flow 2a has the app queueing the revocation while offline, and by the time
    it runs the access token may well have expired; asking for one would make
    the queued revocation impossible to deliver.
    """

    refresh_token: str = Field(min_length=1)


class VerificacionIn(BaseModel):
    """RF-001 step 5 / RF-006 flow 3a: the token that arrived by e-mail."""

    token: str = Field(min_length=1, max_length=200)


class ReenvioVerificacionIn(BaseModel):
    """RF-001 flow 5a and RF-002 flow 2c: send the verification again."""

    correo: EmailStr


class RecuperacionIn(BaseModel):
    """RF-003 step 1: only the e-mail; the answer never says whether it exists."""

    correo: EmailStr


class RestablecerPasswordIn(BaseModel):
    """RF-003 step 3: the token plus the new password, captured twice."""

    token: str = Field(min_length=1, max_length=200)
    password: str = Field(max_length=128)
    password_confirmacion: str = Field(max_length=128)

    @field_validator("password")
    @classmethod
    def _password(cls, valor: str) -> str:
        return validar_password(valor)

    @model_validator(mode="after")
    def _coinciden(self) -> "RestablecerPasswordIn":
        if self.password != self.password_confirmacion:
            raise ValueError("Las dos contraseñas no coinciden. Vuelve a escribirlas.")
        return self


class MensajeOut(BaseModel):
    """A plain acknowledgement.

    RF-003 flow 2a needs a response that is IDENTICAL whether the account
    exists or not, so it carries a message and nothing else: any field derived
    from the account would be the leak the requirement forbids.
    """

    mensaje: str


class TokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    usuario: UsuarioOut
    permisos: list[str] = Field(default_factory=list)
    #: RF-002 flow 2c: the session is granted and the client is told the
    #: address is still unconfirmed, so it can offer to send the mail again.
    verificacion_pendiente: bool = False
