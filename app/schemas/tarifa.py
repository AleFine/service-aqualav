"""Tariff, package, promotion and add-on payloads (RF-010, RF-011, RF-012).

Every amount crossing this boundary is an integer number of cents inside a
:class:`~app.schemas.common.Dinero` object (P6, RN-12), and the vehicle factor
crosses as ``factor_milesimas`` - an integer - for the same reason: a JSON
``1.3`` read back as a binary float is how ``3000 x 1.3`` stops being 3900.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import (
    FACTOR_BASE_MILESIMAS,
    MONEDA_PREDETERMINADA,
    TipoDescuento,
    TipoVehiculo,
)
from app.schemas.common import Dinero

#: Bounds of a vehicle factor: from 0.1x to 10x. Zero would make the service
#: free by configuration mistake, which RF-012 flow 4a treats as an incident.
FACTOR_MINIMO = 100
FACTOR_MAXIMO = 10_000

#: ``datetime.weekday()`` domain, for ``dias_semana``.
DIA_MINIMO = 0
DIA_MAXIMO = 6

MENSAJE_FACTOR = "El factor debe expresarse en milésimas, entre 100 (0,1x) y 10000 (10x)."
MENSAJE_DIAS = "Los días de la semana van de 0 (lunes) a 6 (domingo)."
MENSAJE_PORCENTAJE = "Un descuento porcentual debe estar entre 1 y 100."
MENSAJE_RANGO = "La fecha de fin de vigencia no puede ser anterior a la de inicio."


def _texto_de_dias(dias: list[int] | None) -> str | None:
    """``[1, 3]`` -> ``"1,3"``; ``None`` or ``[]`` -> ``None`` (every day)."""
    if not dias:
        return None
    return ",".join(str(dia) for dia in sorted(set(dias)))


def validar_dias(dias: list[int] | None) -> list[int] | None:
    if dias is None:
        return None
    if any(dia < DIA_MINIMO or dia > DIA_MAXIMO for dia in dias):
        raise ValueError(MENSAJE_DIAS)
    return dias


# --------------------------------------------------------------------------
# Vehicle factors (RF-010 delta)
# --------------------------------------------------------------------------
class FactorIn(BaseModel):
    """Set the factor of one ``(servicio, tipo de vehículo)`` pair.

    ``servicio_id`` omitted means the GLOBAL factor of that vehicle type, the
    one every service without a row of its own inherits.
    """

    servicio_id: int | None = Field(default=None, ge=1)
    tipo_vehiculo: TipoVehiculo
    factor_milesimas: int = Field(default=FACTOR_BASE_MILESIMAS)

    @field_validator("factor_milesimas")
    @classmethod
    def _factor(cls, valor: int) -> int:
        if valor < FACTOR_MINIMO or valor > FACTOR_MAXIMO:
            raise ValueError(MENSAJE_FACTOR)
        return valor


class FactorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    servicio_id: int | None = None
    servicio: str | None = None
    tipo_vehiculo: str
    factor_milesimas: int
    vigente_desde: datetime


# --------------------------------------------------------------------------
# Add-ons
# --------------------------------------------------------------------------
class AdicionalIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=80)
    descripcion: str | None = Field(default=None, max_length=400)
    monto_centimos: int = Field(gt=0)
    moneda: str = Field(default=MONEDA_PREDETERMINADA, min_length=3, max_length=3)


class AdicionalActualizar(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=80)
    descripcion: str | None = Field(default=None, max_length=400)
    monto_centimos: int | None = Field(default=None, gt=0)
    activo: bool | None = None


class AdicionalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    descripcion: str | None = None
    monto: Dinero
    activo: bool


# --------------------------------------------------------------------------
# Promotions (RF-011)
# --------------------------------------------------------------------------
class PromocionIn(BaseModel):
    """A discount with a validity window.

    ``codigo_cupon`` turns it into an opt-in coupon; without it the promotion
    applies on its own and therefore may not overlap another automatic one over
    the same service (flow 2a).
    """

    nombre: str = Field(min_length=1, max_length=80)
    descripcion: str | None = Field(default=None, max_length=400)
    tipo_descuento: TipoDescuento
    #: Percentage points (1-100) or cents, per ``tipo_descuento``.
    valor: int = Field(gt=0)
    servicio_id: int | None = Field(default=None, ge=1)
    paquete_id: int | None = Field(default=None, ge=1)
    codigo_cupon: str | None = Field(default=None, min_length=3, max_length=30)
    dias_semana: list[int] | None = None
    vigente_desde: date
    vigente_hasta: date | None = None

    @field_validator("codigo_cupon")
    @classmethod
    def _cupon(cls, valor: str | None) -> str | None:
        return valor.strip().upper() if valor else None

    @field_validator("dias_semana")
    @classmethod
    def _dias(cls, valor: list[int] | None) -> list[int] | None:
        return validar_dias(valor)

    @field_validator("valor")
    @classmethod
    def _valor(cls, valor: int, info) -> int:
        tipo = info.data.get("tipo_descuento")
        if tipo == TipoDescuento.PORCENTAJE and valor > 100:
            raise ValueError(MENSAJE_PORCENTAJE)
        return valor

    @field_validator("vigente_hasta")
    @classmethod
    def _rango(cls, valor: date | None, info) -> date | None:
        desde = info.data.get("vigente_desde")
        if valor is not None and desde is not None and valor < desde:
            raise ValueError(MENSAJE_RANGO)
        return valor

    @property
    def dias_semana_texto(self) -> str | None:
        return _texto_de_dias(self.dias_semana)


class PromocionActualizar(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=80)
    descripcion: str | None = Field(default=None, max_length=400)
    valor: int | None = Field(default=None, gt=0)
    dias_semana: list[int] | None = None
    vigente_desde: date | None = None
    vigente_hasta: date | None = None
    activa: bool | None = None

    @field_validator("dias_semana")
    @classmethod
    def _dias(cls, valor: list[int] | None) -> list[int] | None:
        return validar_dias(valor)

    @property
    def dias_semana_texto(self) -> str | None:
        return _texto_de_dias(self.dias_semana)


class PromocionResumen(BaseModel):
    """The promotion as the catalogue shows it next to the regular price."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    tipo_descuento: str
    valor: int
    vigente_hasta: date | None = None


class PromocionOut(PromocionResumen):
    descripcion: str | None = None
    servicio_id: int | None = None
    paquete_id: int | None = None
    codigo_cupon: str | None = None
    dias_semana: list[int] = Field(default_factory=list)
    vigente_desde: date
    activa: bool
    #: Derived, never stored: ``vigente_hasta`` is compared against today.
    vigente: bool = True


# --------------------------------------------------------------------------
# Packages (RF-011)
# --------------------------------------------------------------------------
class PaqueteLineaIn(BaseModel):
    servicio_id: int = Field(ge=1)
    cantidad: int = Field(default=1, ge=1, le=10)


class PaqueteLineaOut(BaseModel):
    servicio_id: int
    nombre: str
    cantidad: int
    precio: Dinero


class PaqueteIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=80)
    descripcion: str = Field(min_length=1, max_length=400)
    precio_centimos: int = Field(gt=0)
    moneda: str = Field(default=MONEDA_PREDETERMINADA, min_length=3, max_length=3)
    vigente_desde: date
    vigente_hasta: date | None = None
    servicios: list[PaqueteLineaIn] = Field(min_length=1)

    @field_validator("vigente_hasta")
    @classmethod
    def _rango(cls, valor: date | None, info) -> date | None:
        desde = info.data.get("vigente_desde")
        if valor is not None and desde is not None and valor < desde:
            raise ValueError(MENSAJE_RANGO)
        return valor


class PaqueteActualizar(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=80)
    descripcion: str | None = Field(default=None, min_length=1, max_length=400)
    precio_centimos: int | None = Field(default=None, gt=0)
    vigente_desde: date | None = None
    vigente_hasta: date | None = None
    activo: bool | None = None
    servicios: list[PaqueteLineaIn] | None = None


class PaqueteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    descripcion: str
    precio: Dinero
    #: Sum of the regular prices of its lines, so the app can show the saving.
    precio_regular: Dinero
    #: Both present only when a promotion also discounts the package (RF-011):
    #: the catalogue shows the promotional price next to the regular one, and
    #: naming the promotion is what lets the app explain the difference.
    precio_promocional: Dinero | None = None
    promocion: PromocionResumen | None = None
    activo: bool
    vigente_desde: date
    vigente_hasta: date | None = None
    servicios: list[PaqueteLineaOut] = Field(default_factory=list)


# --------------------------------------------------------------------------
# The breakdown (RF-012)
# --------------------------------------------------------------------------
class AdicionalAplicadoOut(BaseModel):
    servicio_adicional_id: int | None = None
    nombre: str
    monto: Dinero


class DesgloseOut(BaseModel):
    """RN-04 term by term: this is the "trazable y auditable" of RF-012."""

    precio_base: Dinero
    tipo_vehiculo: str | None = None
    factor_milesimas: int
    base_ajustada: Dinero
    adicionales: list[AdicionalAplicadoOut] = Field(default_factory=list)
    adicionales_total: Dinero
    descuento: Dinero
    total: Dinero
    promocion_id: int | None = None
    promocion: str | None = None
    cupon_aplicado: str | None = None
    #: RF-012 flow 3a: what was rejected and why, without failing the request.
    cupon_rechazado: str | None = None
    motivo_rechazo_cupon: str | None = None
    #: RF-012 flow 4a: present when the total had to be clamped to zero.
    incidencia: str | None = None


class TarifaCalculoIn(BaseModel):
    """Body of ``POST /tarifas/calculo``: the quote before committing to it."""

    servicio_id: int = Field(ge=1)
    vehiculo_id: int | None = Field(default=None, ge=1)
    tipo_vehiculo: TipoVehiculo | None = None
    adicionales: list[int] = Field(default_factory=list)
    cupon: str | None = Field(default=None, max_length=30)
    #: Day the service would happen; promotions are evaluated against it.
    fecha: date | None = None
