"""User payloads. ``hash_password`` is never exposed."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.bahia import BahiaResumen
from app.schemas.common import normalizar_telefono


class UsuarioOut(BaseModel):
    """Public projection of ``usuario``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    nombres: str
    apellidos: str
    correo: str
    telefono: str
    rol: str
    rol_id: int
    estado_cuenta: str
    # RF-035: the bay this worker usually takes. It only pre-selects the
    # suggestion of RF-020; it never reserves the bay for them.
    bahia_habitual: BahiaResumen | None = None
    creado_en: datetime

    @field_validator("rol", mode="before")
    @classmethod
    def _nombre_del_rol(cls, valor: Any) -> Any:
        """Accept either the Rol instance or an already flattened name."""
        nombre = getattr(valor, "nombre", None)
        return nombre if nombre is not None else valor


class ClienteResumen(BaseModel):
    """Customer summary nested inside a reservation payload."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    nombres: str
    apellidos: str
    telefono: str


class UsuarioInternoCrear(BaseModel):
    """Body of ``POST /admin/usuarios`` (RF-035).

    There is no password field on purpose: the administrator never chooses it.
    The system generates a temporary one and the mail provider delivers it, so
    a credential is never typed into a screen somebody else is looking at.
    """

    nombres: str = Field(min_length=1, max_length=80)
    apellidos: str = Field(min_length=1, max_length=80)
    correo: EmailStr
    telefono: str = Field(max_length=20)
    #: From ``GET /admin/roles``: a role travels as an ID, never as a name (P5).
    rol_id: int = Field(ge=1)
    bahia_habitual_id: int | None = Field(default=None, ge=1)

    @field_validator("nombres", "apellidos")
    @classmethod
    def _limpiar(cls, valor: str) -> str:
        return valor.strip()

    @field_validator("telefono")
    @classmethod
    def _telefono(cls, valor: str) -> str:
        return normalizar_telefono(valor)


class UsuarioInternoActualizar(BaseModel):
    """Any subset of the editable fields of an internal account (RF-035)."""

    nombres: str | None = Field(default=None, min_length=1, max_length=80)
    apellidos: str | None = Field(default=None, min_length=1, max_length=80)
    telefono: str | None = Field(default=None, max_length=20)
    rol_id: int | None = Field(default=None, ge=1)
    bahia_habitual_id: int | None = Field(default=None, ge=1)
    #: ``False`` deactivates the account: the login answers 403 from then on
    #: (CA-01) and the change is refused while the worker holds services (4a).
    activa: bool | None = None

    @field_validator("nombres", "apellidos")
    @classmethod
    def _limpiar(cls, valor: str | None) -> str | None:
        return valor.strip() if valor is not None else None

    @field_validator("telefono")
    @classmethod
    def _telefono(cls, valor: str | None) -> str | None:
        return None if valor is None else normalizar_telefono(valor)
