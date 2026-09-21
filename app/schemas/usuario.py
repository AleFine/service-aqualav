"""User payloads. ``hash_password`` is never exposed."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import Idioma
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
    # RF-001 v1.0 / RF-006: what the profile screen shows and edits.
    tipo_documento: str | None = None
    numero_documento: str | None = None
    foto_perfil_key: str | None = None
    idioma: str = Idioma.ES.value
    notificar_push: bool = True
    notificar_correo: bool = True
    # RF-035: the bay this worker usually takes. It only pre-selects the
    # suggestion of RF-020; it never reserves the bay for them.
    bahia_habitual: BahiaResumen | None = None
    # RF-031 output: "promedio del OPERARIO actualizado". Zero and ``None`` for
    # everybody who never worked a service, which is most of the table.
    calificacion_promedio: float | None = None
    calificaciones_count: int = 0
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


class PerfilActualizar(BaseModel):
    """Body of ``PATCH /perfil`` (RF-006).

    Every field is optional: this is a PATCH, so what the customer did not
    send is what they did not touch. ``correo`` is accepted but NOT applied
    (CA-02): it opens a verification of the new address and the old one keeps
    working until the link in that mail is used.

    Flow 4a - "a validation error points at the field and keeps the rest of
    what was typed" - falls out of this shape: a rejected request applies
    NOTHING, so the values the customer already had are still the ones on the
    server and the form can be resubmitted with only the offending field fixed.
    """

    nombres: str | None = Field(default=None, min_length=1, max_length=80)
    apellidos: str | None = Field(default=None, min_length=1, max_length=80)
    telefono: str | None = Field(default=None, max_length=20)
    correo: EmailStr | None = None
    foto_perfil_key: str | None = Field(default=None, max_length=300)
    idioma: Idioma | None = None
    notificar_push: bool | None = None
    notificar_correo: bool | None = None

    @field_validator("nombres", "apellidos")
    @classmethod
    def _limpiar(cls, valor: str | None) -> str | None:
        return valor.strip() if valor is not None else None

    @field_validator("telefono")
    @classmethod
    def _telefono(cls, valor: str | None) -> str | None:
        return None if valor is None else normalizar_telefono(valor)


class PerfilOut(BaseModel):
    """``UsuarioOut`` plus what RF-006 CA-02 needs the screen to explain.

    ``correo`` is always the address in force. ``correo_pendiente`` is the one
    waiting for its link to be used; while it is not ``None`` the screen shows
    "we sent a mail to X" and the previous address keeps signing in.
    """

    model_config = ConfigDict(from_attributes=True)

    usuario: UsuarioOut
    correo_pendiente: str | None = None
    verificacion_pendiente: bool = False
