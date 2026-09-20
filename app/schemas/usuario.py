"""User payloads. ``hash_password`` is never exposed."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class UsuarioOut(BaseModel):
    """Public projection of ``usuario``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    nombres: str
    apellidos: str
    correo: str
    telefono: str
    rol: str
    estado_cuenta: str
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
