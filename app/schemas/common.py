"""Shared building blocks for every response payload."""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import MONEDA_PREDETERMINADA

T = TypeVar("T")


class Dinero(BaseModel):
    """Money is always an object: integer cents plus currency. Never a float."""

    model_config = ConfigDict(from_attributes=True)

    monto_centimos: int
    moneda: str = MONEDA_PREDETERMINADA

    @classmethod
    def de_centimos(cls, monto_centimos: int, moneda: str = MONEDA_PREDETERMINADA) -> "Dinero":
        """Build the money object from the two flat columns models store."""
        return cls(monto_centimos=monto_centimos, moneda=moneda)


def nombre_de_autor(valor: object) -> object:
    """Flatten a Usuario instance into its display name, leaving str/None alone."""
    nombre_completo = getattr(valor, "nombre_completo", None)
    return nombre_completo if nombre_completo is not None else valor


class DetalleError(BaseModel):
    """One entry of the ``detalles`` array of an error body."""

    campo: str | None = None
    mensaje: str


class ContenidoError(BaseModel):
    codigo: str
    mensaje: str
    detalles: list[DetalleError] = Field(default_factory=list)


class ErrorBody(BaseModel):
    """The uniform error body documented for every 4xx/5xx response."""

    error: ContenidoError


class Lista(BaseModel, Generic[T]):
    """Non paginated collection: ``{"items": [...]}``."""

    items: list[T] = Field(default_factory=list)


class Pagina(BaseModel, Generic[T]):
    """Paginated collection (RF-017)."""

    items: list[T] = Field(default_factory=list)
    pagina: int = 1
    tamanio: int = 20
    total: int = 0
    total_paginas: int = 0


#: Pagination defaults and bounds (RF-017 CA-01).
TAMANIO_PAGINA_DEFECTO = 20
TAMANIO_PAGINA_MAXIMO = 50
