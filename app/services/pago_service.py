"""Payment registration at the counter (RF-026).

EXTENSION POINT P6: the idempotency key is REQUIRED from the MVP even though
nothing retries yet. It is the piece that prevents a double charge when the
gateway arrives in v0.3, and adding it afterwards would mean migrating every
payment already registered.
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import DatosInvalidos, IdempotencyKeyRequerida, detalle
from app.core.horario import ahora_utc
from app.models import EstadoPago, Pago, Reserva, Usuario
from app.repositories import pago as pago_repo
from app.schemas import PagoCrear
from app.services import eventos


def _resolver_clave(datos: PagoCrear, idempotency_key: str | None) -> str:
    """The header wins; the body field is the documented fallback."""
    clave = (idempotency_key or datos.idempotency_key or "").strip()
    if not clave:
        raise IdempotencyKeyRequerida(
            detalles=[detalle("Idempotency-Key", "Envía una clave única por cada cobro.")]
        )
    return clave[:80]


def registrar(
    db: Session,
    reserva: Reserva,
    datos: PagoCrear,
    autor: Usuario,
    idempotency_key: str | None,
) -> tuple[Pago, bool]:
    """Register a payment, or replay the existing one.

    Returns ``(pago, creado)``. ``creado`` is ``False`` when the key had
    already been used, which is what makes the endpoint answer 200 instead of
    201 without inserting a second row (CA-02).
    """
    clave = _resolver_clave(datos, idempotency_key)

    existente = pago_repo.obtener_por_idempotency_key(db, clave)
    if existente is not None:
        return existente, False

    if reserva.hora_fin_real is None:
        # "The service is over" is a FACT the state machine recorded, not a
        # state name: ``cambiar_estado`` stamps ``hora_fin_real`` when the move
        # it performed carries ``marca_fin_servicio``. Charging therefore keeps
        # working when v0.4 inserts a state between finishing and delivering.
        raise DatosInvalidos(
            "Solo se puede cobrar un servicio terminado. "
            "Avanza el estado antes de registrar el pago.",
            detalles=[detalle("estado", f"Estado actual: «{reserva.estado}».")],
        )

    if datos.monto_centimos != reserva.monto_centimos and not datos.motivo_diferencia:
        # RF-026 flow 3a: a different amount always has to be justified.
        raise DatosInvalidos(
            "El monto cobrado no coincide con el de la reserva. Indica el motivo de la diferencia.",
            detalles=[
                detalle("motivo_diferencia", "Obligatorio cuando el monto difiere del esperado."),
                detalle("monto_centimos", f"Monto esperado: {reserva.monto_centimos}."),
            ],
        )

    try:
        pago = pago_repo.crear(
            db,
            reserva_id=reserva.id,
            monto_centimos=datos.monto_centimos,
            moneda=reserva.moneda,
            medio=datos.medio.value,
            estado=EstadoPago.CONFIRMADO.value,
            idempotency_key=clave,
            motivo_diferencia=datos.motivo_diferencia,
            autor_id=autor.id,
            registrado_en=ahora_utc(),
        )
    except IntegrityError:
        # Two requests raced with the same key: the loser replays the winner.
        db.rollback()
        existente = pago_repo.obtener_por_idempotency_key(db, clave)
        if existente is None:  # pragma: no cover - only a genuine constraint bug
            raise
        return existente, False

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_PAGO,
        pago.id,
        eventos.PAGO_REGISTRADO,
        autor_id=autor.id,
        datos={
            "reserva_id": reserva.id,
            "monto_centimos": pago.monto_centimos,
            "medio": pago.medio,
            "idempotency_key": clave,
        },
    )
    db.commit()
    db.refresh(pago)
    return pago, True
