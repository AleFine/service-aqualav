"""Role and permission payloads (RF-004)."""

from pydantic import BaseModel, ConfigDict, Field


class PermisoOut(BaseModel):
    """One permission code with the sentence that explains it."""

    model_config = ConfigDict(from_attributes=True)

    codigo: str
    descripcion: str | None = None


class RolOut(BaseModel):
    """A role and the permission codes it grants.

    The screen renders what a role can do from ``permisos``; it never infers it
    from ``nombre``, which is a label for humans (principle P5).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    descripcion: str | None = None
    permisos: list[str] = Field(default_factory=list)


class AsignacionRolIn(BaseModel):
    """Body of ``PUT /admin/usuarios/{usuario_id}/rol``.

    The role travels as an ID taken from ``GET /admin/roles``, so no role name
    ever crosses the API boundary and no client can branch on one.
    """

    rol_id: int = Field(..., ge=1, description="Identificador del rol, de GET /admin/roles.")
