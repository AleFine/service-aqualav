"""Roles and permissions (P5: authorize by permission, never by role name)."""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.usuario import Usuario


class Rol(Base):
    __tablename__ = "rol"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    descripcion: Mapped[str | None] = mapped_column(String(200), nullable=True)

    permisos: Mapped[list["Permiso"]] = relationship(
        "Permiso", secondary="rol_permiso", back_populates="roles", lazy="selectin"
    )
    usuarios: Mapped[list["Usuario"]] = relationship("Usuario", back_populates="rol")

    @property
    def codigos_permisos(self) -> list[str]:
        """Permission codes granted by this role, sorted for stable payloads."""
        return sorted(permiso.codigo for permiso in self.permisos)


class Permiso(Base):
    __tablename__ = "permiso"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    descripcion: Mapped[str | None] = mapped_column(String(200), nullable=True)

    roles: Mapped[list["Rol"]] = relationship(
        "Rol", secondary="rol_permiso", back_populates="permisos"
    )


class RolPermiso(Base):
    """Association table between roles and permissions."""

    __tablename__ = "rol_permiso"

    rol_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("rol.id", ondelete="CASCADE"), primary_key=True
    )
    permiso_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("permiso.id", ondelete="CASCADE"), primary_key=True
    )
