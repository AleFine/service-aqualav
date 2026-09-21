"""Loyalty programme payloads (RF-032, RN-11)."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Dinero


class BeneficioOut(BaseModel):
    """One benefit on offer. Exhausted or expired ones never reach here (4b)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    descripcion: str | None = None
    puntos_requeridos: int
    #: Which service it gives away, when it is tied to one. ``None`` means the
    #: benefit applies to any service the coupon is used on.
    servicio_id: int | None = None
    servicio: str | None = None
    #: What that service costs today, so the customer can see what their points
    #: are worth. Informative: the coupon discounts 100 %, whatever the price
    #: and the vehicle factor turn out to be on the day they book.
    valor_referencial: Dinero | None = None
    #: ``None`` = unlimited. An integer is what is left (RF-032 flow 4b).
    stock: int | None = None
    vigente_hasta: date | None = None
    #: Whether THIS customer can afford it right now, and what is missing. The
    #: listing answers it so the app never has to subtract on its own.
    alcanzable: bool = True
    puntos_faltantes: int | None = None


class MovimientoPuntosOut(BaseModel):
    """One line of the statement (RF-032 "saldo y movimientos de puntos")."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tipo: str
    #: Signed: positive when points came in, negative when they went out.
    puntos: int
    saldo_resultante: int
    reserva_id: int | None = None
    #: RN-11 made checkable: what was billed to produce these points.
    base: Dinero | None = None
    beneficio: str | None = None
    ocurrido_en: datetime


class CuponCanjeOut(BaseModel):
    """The coupon a redemption produced (RF-032 "Salidas")."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    codigo: str
    beneficio: str
    estado: str
    vence_en: datetime
    emitido_en: datetime
    reserva_id: int | None = None
    usado_en: datetime | None = None


class SaldoPuntosOut(BaseModel):
    """Everything the loyalty screen needs, in one request (RF-032)."""

    puntos: int
    #: How many more points the cheapest benefit on offer needs. ``None`` when
    #: the customer can already redeem something, or when nothing is on offer.
    puntos_para_el_siguiente: int | None = None
    #: RN-11 spelled out for the screen, so the rule is not retyped in the app.
    centimos_por_punto: int
    movimientos: list[MovimientoPuntosOut] = Field(default_factory=list)
    cupones: list[CuponCanjeOut] = Field(default_factory=list)


class CanjeIn(BaseModel):
    """Body of ``POST /fidelizacion/canjes``: which benefit to redeem."""

    beneficio_id: int = Field(ge=1)
