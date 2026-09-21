"""The simulated scheduler (plan section 4, RF-030).

Two shapes of the same work:

* :func:`ejecutar_pendientes` is a PURE function of ``(db, momento)``. It takes
  the instant as an argument, so a test says "son las 13:00" instead of waiting
  for 13:00, and the same call is what the internal endpoint runs. It is the
  whole scheduler; everything else here is plumbing around it.
* :func:`iniciar_bucle` is the background loop that calls it every so often.
  It is switched off by configuration (``PLANIFICADOR_HABILITADO``) and the
  test suite turns it off explicitly, so nothing in the suite ever depends on
  wall-clock time.

It does three things per sweep:

1. **Reminders (RF-030)** for the reservations starting within the next two
   hours.
2. **Expires unpaid online bookings (RF-014 flow 2a)**, the fifteen minute
   window INC-4 opened. The block goes back on sale by running the ORDINARY
   cancellation - same transition, same event, same notice - on behalf of the
   customer who made the booking, so nothing about it is a special case and
   RN-05 decides the penalty exactly as it would if they had cancelled by
   hand (at that point the service is still hours away, so it is zero).
3. **Promotes the waiting queue (RF-020 flow 2a)**, which INC-1B left as an
   explicit opening: until now a queued vehicle only got its bay when the
   receptionist retried ``POST /reservas/{id}/asignacion`` by hand. The
   promotion runs ON BEHALF of whoever queued it - the author is read back from
   the ``reserva.encolada`` domain event - so it uses that person's permissions
   and never invents a system account with rights nobody granted. When the
   suggestion would need a human decision (the operator is already busy,
   RF-020 flow 3a) the row simply stays in the queue: a scheduler must not
   confirm on somebody's behalf.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import AppError
from app.core.horario import ahora_utc
from app.database import SessionLocal
from app.models import Recordatorio, Usuario
from app.repositories import asignacion as asignacion_repo
from app.repositories import evento as evento_repo
from app.repositories import reserva as reserva_repo
from app.repositories import transicion as transicion_repo
from app.repositories import usuario as usuario_repo
from app.schemas import AsignacionIn
from app.services import (
    asignacion_service,
    eventos,
    operacion_service,
    recordatorio_service,
    reserva_service,
)

logger = logging.getLogger("aqualav.planificador")

#: RF-014 flow 2a: what the customer reads on a booking the timer cancelled.
MOTIVO_EXPIRACION = "Venció el plazo de 15 minutos para completar el pago en línea de la reserva."


@dataclass
class ResultadoPlanificador:
    """What one sweep did. Empty lists are a perfectly good outcome."""

    momento: datetime
    recordatorios: list[Recordatorio] = field(default_factory=list)
    promovidas: list[int] = field(default_factory=list)
    #: RF-014 flow 2a: ids of the bookings whose payment window ran out.
    expiradas: list[int] = field(default_factory=list)


# --------------------------------------------------------------------------
# RF-030: the reminder sweep
# --------------------------------------------------------------------------
def _recordar(db: Session, momento: datetime, **proveedores) -> list[Recordatorio]:
    """Remind every reservation starting within the next two hours."""
    limite = momento + recordatorio_service.VENTANA_RECORDATORIO
    activos = transicion_repo.listar_estados_no_terminales(db)
    enviados: list[Recordatorio] = []

    for reserva in reserva_repo.listar_por_inicio_entre(db, momento, limite, activos):
        fila = recordatorio_service.enviar(db, reserva, momento=momento, **proveedores)
        if fila is not None:
            enviados.append(fila)

    return enviados


# --------------------------------------------------------------------------
# RF-014 flow 2a: the fifteen minute payment window
# --------------------------------------------------------------------------
def _caducar_pagos(db: Session, momento: datetime, **proveedores) -> list[int]:
    """Cancel the online bookings whose payment window closed.

    WHICH states are still waiting for the money is read from
    ``transicion_estado`` - the states the payment operation owns a move out of
    - so this sweep names no state (P3) and a waiting state added as data is
    swept by itself.

    The cancellation runs as the CUSTOMER who booked it: they hold
    ``reserva:cancelar``, it is their reservation, and the history shows who
    the booking belonged to instead of a system account with rights nobody
    granted. Same reasoning as ``_autor_de_la_cola`` right below.
    """
    esperando = transicion_repo.listar_estados_con_endpoint(db, operacion_service.ENDPOINT_PAGO)
    expiradas: list[int] = []

    for reserva in reserva_repo.listar_expiradas(db, momento, esperando):
        autor = reserva.usuario
        if autor is None:  # pragma: no cover - a reservation always has an owner
            continue
        try:
            reserva_service.cancelar(
                db,
                reserva,
                MOTIVO_EXPIRACION,
                autor,
                autor.rol.codigos_permisos,
                **proveedores,
            )
        except AppError as error:
            # Somebody got there first, or the table no longer declares the
            # move. Either way the sweep is not the place to argue about it.
            logger.info("reserva no caducada reserva=%s motivo=%s", reserva.id, error.codigo)
            continue

        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_RESERVA,
            reserva.id,
            eventos.RESERVA_PAGO_EXPIRADO,
            autor_id=autor.id,
            datos={"codigo": reserva.codigo, "expiro_en": momento.isoformat()},
        )
        expiradas.append(reserva.id)

    return expiradas


# --------------------------------------------------------------------------
# RF-020 flow 2a: promoting the waiting queue
# --------------------------------------------------------------------------
def _autor_de_la_cola(db: Session, reserva_id: int) -> Usuario | None:
    """Who put this reservation in the queue, read back from the event log.

    P7 pays for itself here: ``reserva.encolada`` recorded the receptionist,
    so the promotion can run with THEIR permissions instead of a fabricated
    superuser. If the trail is gone the row simply is not promoted.
    """
    for evento in reversed(evento_repo.listar_por_entidad(db, eventos.ENTIDAD_RESERVA, reserva_id)):
        if evento.accion == eventos.RESERVA_ENCOLADA and evento.autor_id is not None:
            return usuario_repo.obtener_por_id(db, evento.autor_id)
    return None


def _promover_cola(db: Session, **proveedores) -> list[int]:
    """Assign the queued vehicles a bay freed up in the meantime."""
    promovidas: list[int] = []

    for fila in list(asignacion_repo.listar_cola(db)):
        reserva = fila.reserva
        if reserva is None:
            continue

        autor = _autor_de_la_cola(db, reserva.id)
        if autor is None:
            continue

        permisos = autor.rol.codigos_permisos
        try:
            resultado = asignacion_service.asignar(
                db, reserva, AsignacionIn(), autor, permisos, **proveedores
            )
        except AppError as error:
            # Flow 3a asks a HUMAN to confirm a busy operator, and any other
            # refusal is a decision the counter has to see. Leave the row where
            # it is and move on; the next sweep tries again.
            logger.info("cola no promovida reserva=%s motivo=%s", reserva.id, error.codigo)
            continue

        if resultado.asignacion is None:
            # Still no bay: everybody behind is waiting for the same thing.
            break

        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_RESERVA,
            reserva.id,
            eventos.RESERVA_PROMOVIDA_DE_COLA,
            autor_id=autor.id,
            datos={"bahia_id": resultado.asignacion.bahia_id},
        )
        promovidas.append(reserva.id)

    return promovidas


# --------------------------------------------------------------------------
# The scheduler itself
# --------------------------------------------------------------------------
def ejecutar_pendientes(
    db: Session,
    momento: datetime | None = None,
    **proveedores,
) -> ResultadoPlanificador:
    """Run one sweep as if it were ``momento`` (plan section 4).

    Pure with respect to the clock: the only default is ``ahora_utc()``, which
    is the same source every other service already uses.
    """
    momento = momento or ahora_utc()

    recordatorios = _recordar(db, momento, **proveedores)
    # Before the queue: an expired booking frees a bay, and the promotion that
    # follows can hand it to whoever is waiting in the very same sweep.
    expiradas = _caducar_pagos(db, momento, **proveedores)
    promovidas = _promover_cola(db, **proveedores)

    db.commit()
    return ResultadoPlanificador(
        momento=momento,
        recordatorios=recordatorios,
        promovidas=promovidas,
        expiradas=expiradas,
    )


def ejecutar_en_sesion_propia() -> ResultadoPlanificador:
    """Open a session, sweep, close it. Only the background loop uses this."""
    db = SessionLocal()
    try:
        return ejecutar_pendientes(db)
    finally:
        db.close()


# --------------------------------------------------------------------------
# The optional background loop
# --------------------------------------------------------------------------
async def _bucle() -> None:  # pragma: no cover - exercised by running the API
    """Sweep every ``PLANIFICADOR_INTERVALO_SEGUNDOS`` until cancelled."""
    while True:
        await asyncio.sleep(settings.planificador_intervalo_segundos)
        try:
            resultado = await asyncio.to_thread(ejecutar_en_sesion_propia)
        except Exception as error:  # noqa: BLE001 - the loop must survive anything
            logger.warning("El barrido del planificador falló: %s", error)
            continue
        if resultado.recordatorios or resultado.promovidas or resultado.expiradas:
            logger.info(
                "barrido: %s recordatorio(s), %s caducada(s), %s reserva(s) promovida(s)",
                len(resultado.recordatorios),
                len(resultado.expiradas),
                len(resultado.promovidas),
            )


def iniciar_bucle() -> "asyncio.Task | None":
    """Start the loop, or nothing at all when it is switched off.

    Off is a legitimate configuration and not a degraded mode: the sweep is
    always reachable through ``POST /interno/planificador``, which is how the
    tests and a demo run it.
    """
    if not settings.planificador_habilitado:
        logger.info("Planificador de fondo desactivado por configuración.")
        return None
    return asyncio.create_task(_bucle())


async def detener_bucle(tarea: "asyncio.Task | None") -> None:
    """Cancel the loop on shutdown, waiting for it to actually stop."""
    if tarea is None:
        return
    tarea.cancel()
    try:
        await tarea
    except asyncio.CancelledError:  # pragma: no cover - the expected outcome
        pass
