"""Payments, gateway calls, receipts and refunds (RF-026, RF-027, RF-028).

The MVP had one table here because there was one way to pay: the counter took
the money and wrote it down. v1.0 adds a gateway, so three more rows exist for
every online charge, and each of them answers a question the single ``pago``
row could not:

* ``transaccion_pasarela`` - what we ASKED the gateway and what it answered,
  keyed by the idempotency key. It is what makes RF-026 flow 3b possible: when
  the reply never arrives, the state is looked up with the same key instead of
  charging twice;
* ``comprobante`` - the correlative receipt of RF-027, stored as a file in the
  object store and readable from the application even when the mail bounced;
* ``reembolso`` - the reversal of RF-028, including the one the gateway refused
  and a human has to finish by hand.

RNF-013 runs through all of it: **the card number is never stored**. What
travels past ``pago_service`` is a token, and ``Pago.token_tarjeta`` is that
token.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import (
    MONEDA_PREDETERMINADA,
    EstadoComprobante,
    EstadoPago,
    EstadoReembolso,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.reserva import Reserva
    from app.models.usuario import Usuario

#: Same variant phase 1 chose for ``evento_dominio.datos``: JSONB on
#: PostgreSQL, plain JSON on the SQLite the suite runs against.
CUERPO_JSON = JSON().with_variant(JSONB, "postgresql")


class Pago(Base):
    __tablename__ = "pago"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reserva_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reserva.id", ondelete="CASCADE"), index=True, nullable=False
    )
    monto_centimos: Mapped[int] = mapped_column(Integer, nullable=False)
    #: RF-028: what is left to give back. It starts as ``monto_centimos`` and
    #: every processed refund reduces it, which is literally what CA-01 checks.
    #: Keeping it as a column rather than summing the refunds on every read is
    #: what lets the "more than what was paid" rejection (CA-02) be one
    #: comparison inside the transaction that is about to spend it.
    saldo_centimos: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    moneda: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default=MONEDA_PREDETERMINADA,
        server_default=MONEDA_PREDETERMINADA,
    )
    # efectivo | tarjeta_pos | transferencia (counter) - tarjeta | billetera (gateway)
    medio: Mapped[str] = mapped_column(String(20), nullable=False)
    estado: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EstadoPago.CONFIRMADO.value,
        server_default=EstadoPago.CONFIRMADO.value,
    )
    # EXTENSION POINT P6: replaying the same key returns the existing payment.
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    #: RF-026 step 4: "registrar la transacción con su identificador externo".
    #: NULL on a counter payment - there is no gateway behind cash.
    referencia_externa: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Which gateway took it. NULL means the counter did, and that is the flag
    #: RF-016 reads to decide whether a cancellation can reverse the charge by
    #: itself or whether the money goes back over the counter.
    pasarela: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: RNF-013 M3: **never the PAN**. ``tok_<prefijo>_<huella>`` - four digits
    #: of the test BIN so the simulation stays deterministic on the reversal,
    #: and a one-way fingerprint for the rest. Nothing in the system can turn
    #: this back into a card number.
    token_tarjeta: Mapped[str | None] = mapped_column(String(60), nullable=True)
    #: RF-026 flow 3a: why the gateway said no. User facing, kept on the row so
    #: the customer can be told what to fix before retrying with another means.
    motivo_rechazo: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Required when monto_centimos differs from reserva.monto_centimos.
    motivo_diferencia: Mapped[str | None] = mapped_column(String(300), nullable=True)
    autor_id: Mapped[int] = mapped_column(Integer, ForeignKey("usuario.id"), nullable=False)
    registrado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    reserva: Mapped["Reserva"] = relationship("Reserva", back_populates="pagos")
    autor: Mapped["Usuario"] = relationship("Usuario", lazy="joined")
    comprobante: Mapped[Optional["Comprobante"]] = relationship(
        "Comprobante",
        back_populates="pago",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )
    reembolsos: Mapped[list["Reembolso"]] = relationship(
        "Reembolso",
        back_populates="pago",
        cascade="all, delete-orphan",
        order_by="Reembolso.id",
        lazy="selectin",
    )


class TransaccionPasarela(Base):
    """One call to the payment gateway and what it answered (RF-026).

    This is the table RF-026 flow 3b hangs from. The row is written BEFORE the
    call leaves, so a request that times out still left a trace, and the
    idempotency key is unique across the table, so asking again with the same
    key finds the same row instead of starting a second charge.

    ``solicitud`` and ``respuesta`` are JSON for the same reason
    ``evento_dominio.datos`` is: a gateway payload is a document, not a schema
    this project gets to design. Neither of them ever carries a card number
    (RNF-013) - the request holds the token, exactly like ``Pago``.
    """

    __tablename__ = "transaccion_pasarela"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Stamped by the service once the payment row exists. NULL while the call
    #: is in flight and on a call that never became a payment (a rejection).
    pago_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("pago.id", ondelete="CASCADE"), index=True, nullable=True
    )
    reserva_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reserva.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: RNF-017 M1: mandatory on the charge AND on the reversal.
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    # cobro | reembolso
    operacion: Mapped[str] = mapped_column(String(20), nullable=False)
    # aprobada | rechazada | pendiente | tiempo_de_espera
    estado: Mapped[str] = mapped_column(String(20), nullable=False)
    monto_centimos: Mapped[int] = mapped_column(Integer, nullable=False)
    moneda: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default=MONEDA_PREDETERMINADA,
        server_default=MONEDA_PREDETERMINADA,
    )
    referencia_externa: Mapped[str | None] = mapped_column(String(120), nullable=True)
    motivo: Mapped[str | None] = mapped_column(String(300), nullable=True)
    solicitud: Mapped[dict] = mapped_column(CUERPO_JSON, nullable=False, default=dict)
    respuesta: Mapped[dict] = mapped_column(CUERPO_JSON, nullable=False, default=dict)
    intentos: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    ocurrido_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    pago: Mapped[Optional["Pago"]] = relationship("Pago")
    reserva: Mapped["Reserva"] = relationship("Reserva")


class Comprobante(Base):
    """The electronic receipt of a confirmed payment (RF-027).

    ``serie`` plus ``numero_correlativo`` is the number CA-01 asks for, and the
    unique constraint over the pair is what makes it a SERIES: two receipts can
    never share a number, and a number is never reused - a full reversal marks
    the receipt ``anulado`` and the next one keeps counting.

    ``archivo_key`` is the key in the object store, not a path: where the bytes
    actually sit is the storage provider's business, and swapping the local
    simulation for anything else must not rewrite this column.
    """

    __tablename__ = "comprobante"
    __table_args__ = (
        UniqueConstraint("serie", "numero_correlativo", name="uq_comprobante_serie_numero"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reserva_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reserva.id", ondelete="CASCADE"), index=True, nullable=False
    )
    pago_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pago.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    serie: Mapped[str] = mapped_column(String(4), nullable=False)
    numero_correlativo: Mapped[int] = mapped_column(Integer, nullable=False)
    monto_centimos: Mapped[int] = mapped_column(Integer, nullable=False)
    moneda: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default=MONEDA_PREDETERMINADA,
        server_default=MONEDA_PREDETERMINADA,
    )
    medio_pago: Mapped[str] = mapped_column(String(20), nullable=False)
    archivo_key: Mapped[str] = mapped_column(String(200), nullable=False)
    estado: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EstadoComprobante.EMITIDO.value,
        server_default=EstadoComprobante.EMITIDO.value,
    )
    emitido_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    reserva: Mapped["Reserva"] = relationship("Reserva", back_populates="comprobantes")
    pago: Mapped["Pago"] = relationship("Pago", back_populates="comprobante")

    @property
    def numero(self) -> str:
        """``B001-00000001``: what is printed on the document (RF-027 CA-01)."""
        return f"{self.serie}-{self.numero_correlativo:08d}"


class Reembolso(Base):
    """One reversal request and how it ended (RF-028).

    A refused reversal is still a row: flow 3a says the request "queda
    registrada como pendiente de gestión manual", so nothing here is ever
    deleted and ``estado`` is the only thing that moves.
    """

    __tablename__ = "reembolso"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pago_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pago.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # anulacion | total | parcial
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)
    monto_centimos: Mapped[int] = mapped_column(Integer, nullable=False)
    moneda: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default=MONEDA_PREDETERMINADA,
        server_default=MONEDA_PREDETERMINADA,
    )
    motivo: Mapped[str] = mapped_column(String(300), nullable=False)
    estado: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EstadoReembolso.PROCESADO.value,
        server_default=EstadoReembolso.PROCESADO.value,
    )
    referencia_externa: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Why the gateway refused, when it did. It is what the person who has to
    #: finish the reversal by hand reads first.
    detalle: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: RNF-017 M1: "clave de idempotencia obligatoria también en reembolsos".
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    autor_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("usuario.id"), nullable=True)
    registrado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    pago: Mapped["Pago"] = relationship("Pago", back_populates="reembolsos")
    autor: Mapped[Optional["Usuario"]] = relationship("Usuario", lazy="joined")
