"""Payment, receipt and refund payloads (RF-025, RF-026, RF-027, RF-028)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import (
    MEDIOS_EN_LINEA,
    MEDIOS_PRESENCIALES,
    EstadoComprobante,
    EstadoPago,
    EstadoReembolso,
    MedioPago,
    ModalidadPago,
    TipoReembolso,
)
from app.schemas.common import Dinero, nombre_de_autor

MENSAJE_MONTO = "El monto del pago debe ser mayor que cero."
MENSAJE_MEDIO_EN_LINEA = (
    "Ese medio de pago es de la pasarela. Usa el cobro en línea de la reserva, "
    "o elige efectivo, tarjeta_pos o transferencia."
)
MENSAJE_MEDIO_PRESENCIAL = (
    "Ese medio de pago es de caja. Para pagar en línea elige tarjeta o billetera."
)
#: RNF-013 M3: the number is validated for SHAPE and never stored. What is kept
#: is the token ``pasarela.tokenizar`` derives from it.
MENSAJE_TARJETA = "El número de tarjeta debe tener entre 13 y 19 dígitos."


class PagoCrear(BaseModel):
    """Counter charge (RF-026). ``idempotency_key`` also travels in the header."""

    medio: MedioPago
    monto_centimos: int
    motivo_diferencia: str | None = Field(default=None, max_length=300)
    idempotency_key: str | None = Field(default=None, max_length=80)

    @field_validator("monto_centimos")
    @classmethod
    def _monto(cls, valor: int) -> int:
        if valor <= 0:
            raise ValueError(MENSAJE_MONTO)
        return valor

    @field_validator("medio")
    @classmethod
    def _medio(cls, valor: MedioPago) -> MedioPago:
        # RF-025: cash, POS card and transfer are what the counter takes. The
        # two gateway means have their own door and their own invariants.
        if valor.value not in MEDIOS_PRESENCIALES:
            raise ValueError(MENSAJE_MEDIO_EN_LINEA)
        return valor

    @field_validator("motivo_diferencia")
    @classmethod
    def _motivo(cls, valor: str | None) -> str | None:
        if valor is None:
            return None
        limpio = valor.strip()
        return limpio or None


class PagoEnLineaCrear(BaseModel):
    """Gateway charge (RF-026 v1.0).

    The amount is NOT a field: an online payment settles the reservation's own
    total, which INC-2 froze when the booking was made. Letting the client send
    a number would be letting the client decide what the service costs.

    ``numero_tarjeta`` is the TEST card that picks the gateway's behaviour. It
    is turned into a token in the first lines of the service and never stored
    (RNF-013 M3).
    """

    medio: MedioPago = MedioPago.TARJETA
    numero_tarjeta: str = Field(min_length=13, max_length=25)
    idempotency_key: str | None = Field(default=None, max_length=80)

    @field_validator("medio")
    @classmethod
    def _medio(cls, valor: MedioPago) -> MedioPago:
        if valor.value not in MEDIOS_EN_LINEA:
            raise ValueError(MENSAJE_MEDIO_PRESENCIAL)
        return valor

    @field_validator("numero_tarjeta")
    @classmethod
    def _tarjeta(cls, valor: str) -> str:
        digitos = "".join(caracter for caracter in valor if caracter.isdigit())
        if not 13 <= len(digitos) <= 19:
            raise ValueError(MENSAJE_TARJETA)
        return digitos


class ModalidadPagoIn(BaseModel):
    """Body of ``POST /reservas/{id}/modalidad-pago`` (RF-025 flow 4a)."""

    modalidad: ModalidadPago


class PagoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    monto: Dinero
    #: RF-028 CA-01: what is left to give back after the reversals.
    saldo: Dinero | None = None
    medio: MedioPago
    estado: EstadoPago
    registrado_en: datetime
    autor: str | None = None
    #: RF-026 step 4: the gateway's own identifier for the transaction.
    referencia_externa: str | None = None
    pasarela: str | None = None
    #: RF-026 flow 3a: why it was refused, so the app can say it out loud.
    motivo_rechazo: str | None = None

    @field_validator("autor", mode="before")
    @classmethod
    def _autor(cls, valor: object) -> object:
        return nombre_de_autor(valor)


class ComprobanteOut(BaseModel):
    """RF-027: the receipt's metadata plus where to download the PDF."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    numero: str
    serie: str
    numero_correlativo: int
    monto: Dinero
    medio_pago: MedioPago
    estado: EstadoComprobante
    emitido_en: datetime
    #: Where the API serves the PDF from (CA-02).
    archivo_url: str


class ReembolsoCrear(BaseModel):
    """Body of ``POST /pagos/{id}/reembolsos`` (RF-028).

    ``monto_centimos`` is only read for a ``parcial``: an annulment and a total
    reversal always take the whole remaining balance, so letting the caller
    name a different number there would be inviting a mismatch.
    """

    tipo: TipoReembolso = TipoReembolso.PARCIAL
    monto_centimos: int | None = Field(default=None, ge=1)
    motivo: str = Field(min_length=1, max_length=300)
    idempotency_key: str | None = Field(default=None, max_length=80)

    @field_validator("motivo")
    @classmethod
    def _motivo(cls, valor: str) -> str:
        limpio = valor.strip()
        if not limpio:
            raise ValueError("Indica el motivo del reembolso.")
        return limpio


class ReembolsoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    pago_id: int
    tipo: TipoReembolso
    monto: Dinero
    motivo: str
    estado: EstadoReembolso
    referencia_externa: str | None = None
    #: Why it is waiting for a human, when it is (flow 3a).
    detalle: str | None = None
    registrado_en: datetime
    autor: str | None = None

    @field_validator("autor", mode="before")
    @classmethod
    def _autor(cls, valor: object) -> object:
        return nombre_de_autor(valor)
