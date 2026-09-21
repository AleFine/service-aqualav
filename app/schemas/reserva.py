"""Reservation payloads (RF-014, RF-016, RF-017, RF-019, RF-021, RF-022, RF-024)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import EstadoPago, ModalidadPago
from app.schemas.bahia import BahiaResumen
from app.schemas.common import Dinero, nombre_de_autor
from app.schemas.pago import ComprobanteOut, PagoOut
from app.schemas.servicio import ServicioResumen
from app.schemas.tarifa import DesgloseOut
from app.schemas.usuario import ClienteResumen
from app.schemas.vehiculo import VehiculoResumen


class HistorialItem(BaseModel):
    """One row of ``reserva_estado_historial`` as the timeline renders it."""

    model_config = ConfigDict(from_attributes=True)

    # Plain str on purpose: the set of valid states lives in transicion_estado,
    # not in a Python enum (principle P3 / RF-021 CA-03).
    estado: str
    ocurrido_en: datetime
    autor: str | None = None

    @field_validator("autor", mode="before")
    @classmethod
    def _autor(cls, valor: object) -> object:
        return nombre_de_autor(valor)


class CancelacionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    motivo: str | None = None
    cancelada_en: datetime | None = None
    autor: str | None = None

    @field_validator("autor", mode="before")
    @classmethod
    def _autor(cls, valor: object) -> object:
        return nombre_de_autor(valor)


class ReservaCrear(BaseModel):
    """``fin`` is computed server side from the service duration.

    ``adicionales`` and ``cupon`` feed RN-04: the tariff is
    ``(base x factor) + adicionales - descuentos`` and it is frozen into the
    reservation when it is created (RF-012). An invalid coupon does NOT reject
    the booking - it is reported inside the breakdown (flow 3a).
    """

    servicio_id: int
    vehiculo_id: int
    inicio: datetime
    adicionales: list[int] = Field(default_factory=list)
    cupon: str | None = Field(default=None, max_length=30)
    #: RF-025 / RN-08. ``presencial`` is the default because it is what the MVP
    #: did and what a customer who says nothing means: pay at the shop. Choosing
    #: ``en_linea`` is what makes the booking be born waiting for the money and
    #: expire in fifteen minutes (RF-014 flow 2a).
    modalidad_pago: ModalidadPago = ModalidadPago.PRESENCIAL


class CancelacionIn(BaseModel):
    motivo: str = Field(min_length=1, max_length=300)

    @field_validator("motivo")
    @classmethod
    def _motivo(cls, valor: str) -> str:
        limpio = valor.strip()
        if not limpio:
            raise ValueError("Indica el motivo de la cancelación.")
        return limpio


class CheckInIn(BaseModel):
    observaciones: str | None = Field(default=None, max_length=500)
    # The app re-sends with true after a RETRASO_REQUIERE_CONFIRMACION reply.
    confirmar_retraso: bool = False


class CambioEstadoIn(BaseModel):
    # Validated against transicion_estado by the service, never against an enum,
    # so inserting a state row is enough to enable it (RF-021 CA-03).
    estado: str = Field(min_length=1, max_length=30)


class CheckOutIn(BaseModel):
    conformidad_cliente: bool


class RevisionIn(BaseModel):
    """Body of ``POST /reservas/{id}/revision`` (RF-024 flow 3a).

    The observation is mandatory: sending a finished service back to the bay
    without saying what is wrong with it would leave the operator guessing.
    """

    observacion: str = Field(min_length=1, max_length=500)

    @field_validator("observacion")
    @classmethod
    def _observacion(cls, valor: str) -> str:
        limpio = valor.strip()
        if not limpio:
            raise ValueError("Indica qué observó el cliente para enviar el servicio a revisión.")
        return limpio


class AtencionInmediataIn(BaseModel):
    """Body of ``POST /reservas/atencion-inmediata`` (RF-019 flow 1a).

    A walk-in customer: the counter opens the service on the spot, on an
    already registered vehicle, provided a bay is free right now.
    """

    servicio_id: int = Field(ge=1)
    vehiculo_id: int = Field(ge=1)
    observaciones: str | None = Field(default=None, max_length=500)
    adicionales: list[int] = Field(default_factory=list)
    cupon: str | None = Field(default=None, max_length=30)


class ReservaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    codigo: str
    estado: str
    inicio: datetime
    fin: datetime
    creada_en: datetime
    monto: Dinero
    modalidad_pago: str
    #: RF-025 CA-01: "su estado de pago es Pendiente". Derived from the
    #: payments of the reservation, never stored: two places holding the same
    #: fact is how they start disagreeing.
    estado_pago: EstadoPago = EstadoPago.PENDIENTE
    #: RF-014 flow 2a: when an unpaid online booking stops holding its block.
    #: ``None`` once it is paid, cancelled or was never online to begin with.
    expira_en: datetime | None = None
    # RF-019 v1.0: the token the reception ticket's QR encodes. Only a caller
    # holding ``reserva:leer_todas`` needs it - it is a scanning credential.
    codigo_qr: str | None = None
    atencion_sin_reserva: bool = False
    servicio: ServicioResumen
    vehiculo: VehiculoResumen
    bahia: BahiaResumen
    cliente: ClienteResumen
    # Read from transicion_estado and filtered by the caller's permissions:
    # the mobile app renders its action buttons from this array.
    transiciones_permitidas: list[str] = Field(default_factory=list)
    hora_ingreso: datetime | None = None
    hora_fin_real: datetime | None = None
    hora_entrega: datetime | None = None
    fin_estimado: datetime
    # RF-022 v1.0 delta. ``porcentaje_avance`` is the position of the state in
    # the cycle DERIVED from ``transicion_estado``, so a state added as data
    # moves the progress bar by itself; ``hora_estimada_entrega`` is
    # recalculated on every state change and ``minutos_retraso`` is how far it
    # runs past what the booking promised (flow 4a warns past fifteen).
    porcentaje_avance: int = 0
    hora_estimada_entrega: datetime | None = None
    minutos_retraso: int = 0
    # Written at check-in (RF-019). An internal shop note about the state the
    # vehicle arrived in, so it only reaches a caller holding
    # ``reserva:leer_todas``; a customer never sees it.
    observaciones_ingreso: str | None = None
    # Written at check-out (RF-024). Visible to everyone: it is the customer's
    # own answer, and they are entitled to see what was recorded.
    conformidad_cliente: bool | None = None
    # Written when the customer objects at the counter (RF-024 flow 3a).
    observacion_revision: str | None = None
    cancelacion: CancelacionOut | None = None
    pago: PagoOut | None = None
    historial: list[HistorialItem] = Field(default_factory=list)
    # RF-012: why ``monto`` is what it is, frozen when the reservation was
    # created. Absent only on rows created before INC-2.
    tarifa: DesgloseOut | None = None
    #: RF-027 CA-02: the receipt of the service, once there is one.
    comprobante: ComprobanteOut | None = None
    #: RN-05: what the shop kept when the cancellation came in late. Present on
    #: the cancellation response and on every read of a cancelled reservation,
    #: which is what makes the "resumen de la cancelación" of RF-016 step 3
    #: something the customer can look at again.
    penalidad: Dinero | None = None
