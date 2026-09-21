"""The two-hour reminder and what the customer answers to it (RF-030).

The reminder is one row per reservation, which is what makes the sweep
idempotent: running the scheduler twice inside the same window does not send it
twice. Its three actions - confirm, reschedule, cancel - come back through
``POST /reservas/{id}/recordatorio``.

Who still DESERVES a reminder is read from the state machine, never from a list
of states: a reservation deserves one while the check-in operation is still
declared for its current state. A cancelled reservation has no outgoing move at
all, so it is excluded by construction, which is CA-02; one that already
arrived at the counter is excluded for the same reason, which is the sensible
reading of "existe una reserva confirmada para las próximas horas".
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.errors import RecursoNoEncontrado, detalle
from app.core.horario import a_lima, ahora_utc, desde_bd
from app.models import (
    EstadoRecordatorio,
    EventoNotificacion,
    Recordatorio,
    Reserva,
    RespuestaRecordatorio,
    Usuario,
)
from app.repositories import notificacion as notificacion_repo
from app.schemas import RecordatorioRespuestaIn
from app.services import eventos, notificacion_service, operacion_service, reserva_service

#: RF-030: "barrido de reservas que inician EN DOS HORAS".
VENTANA_RECORDATORIO = timedelta(hours=2)

#: Motive stored when the customer cancels straight from the reminder and does
#: not type one. RF-016 CA-03 demands a reason on the record either way.
MOTIVO_POR_DEFECTO = "El cliente canceló desde el recordatorio de su reserva."


def corresponde_recordar(db: Session, reserva: Reserva) -> bool:
    """Whether this reservation is still waiting for its owner to show up."""
    return operacion_service.tiene_operacion_declarada(
        db, reserva, operacion_service.ENDPOINT_CHECK_IN
    )


def enviar(
    db: Session,
    reserva: Reserva,
    *,
    momento: datetime | None = None,
    **proveedores,
) -> Recordatorio | None:
    """Send the reminder of one reservation, once (RF-030 CA-01).

    Returns ``None`` when there is nothing to do: the reminder already went
    out, or the reservation no longer admits a check-in (CA-02).
    """
    if notificacion_repo.obtener_recordatorio(db, reserva.id) is not None:
        return None
    if not corresponde_recordar(db, reserva):
        return None

    momento = momento or ahora_utc()
    fila = notificacion_repo.crear_recordatorio(
        db,
        reserva_id=reserva.id,
        programado_para=desde_bd(reserva.inicio) - VENTANA_RECORDATORIO,
        enviado_en=momento,
        estado=EstadoRecordatorio.ENVIADO.value,
    )

    notificacion_service.despachar(
        db,
        reserva.usuario,
        EventoNotificacion.RECORDATORIO.value,
        reserva=reserva,
        datos={
            # The template names the three actions; this is what the mobile app
            # reads to draw the buttons, so neither of them invents the list.
            "acciones": ", ".join(accion.value for accion in RespuestaRecordatorio),
            "hora": a_lima(desde_bd(reserva.inicio)).strftime(notificacion_service.FORMATO_FECHA),
        },
        momento=momento,
        **proveedores,
    )

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        eventos.RESERVA_RECORDATORIO_ENVIADO,
        datos={"programado_para": fila.programado_para.isoformat()},
    )
    return fila


def obtener(db: Session, reserva: Reserva) -> Recordatorio:
    """The reminder of a reservation, or 404 when none was sent."""
    fila = notificacion_repo.obtener_recordatorio(db, reserva.id)
    if fila is None:
        raise RecursoNoEncontrado(
            "Esa reserva todavía no tiene un recordatorio que responder.",
            detalles=[detalle("reserva_id", "El recordatorio se envía dos horas antes.")],
        )
    return fila


def responder(
    db: Session,
    reserva: Reserva,
    datos: RecordatorioRespuestaIn,
    autor: Usuario,
    permisos: list[str],
) -> tuple[Reserva, Recordatorio]:
    """Register the answer to a reminder and act on it (RF-030).

    * ``confirmo`` - nothing moves, which is the whole point: the reservation
      was already confirmed and stays that way. The answer is recorded so the
      counter knows the customer is coming.
    * ``cancelo`` - the real cancellation runs, with its transition, its reason
      and its penalty policy (flow 4a, RN-05). INC-4 is what makes the penalty
      stop being zero; nothing here has to change for that.
    * ``reprogramo`` - RECORDED INTENT. The actual rescheduling is RF-015 and
      belongs to INC-7 (RN-06: at most twice, more than two hours ahead), and
      inventing half of it here would leave a rule enforced in two places. The
      answer, the moment and the domain event are written now so INC-7 picks
      up a conversation that already started.
    """
    fila = obtener(db, reserva)
    momento = ahora_utc()
    respuesta = datos.respuesta.value

    notificacion_repo.responder_recordatorio(
        db,
        fila,
        respuesta=respuesta,
        estado=EstadoRecordatorio.RESPONDIDO.value,
        momento=momento,
    )

    if respuesta == RespuestaRecordatorio.CANCELO.value:
        motivo = (datos.motivo or "").strip() or MOTIVO_POR_DEFECTO
        reserva, _ = reserva_service.cancelar(db, reserva, motivo, autor, permisos)
        db.refresh(fila)
        return reserva, fila

    accion = (
        eventos.RESERVA_ASISTENCIA_CONFIRMADA
        if respuesta == RespuestaRecordatorio.CONFIRMO.value
        else eventos.RESERVA_REPROGRAMACION_SOLICITADA
    )
    # TODO(INC-7, RF-015): turn ``reprogramo`` into the real move. The call
    # site must not change: this function keeps returning the reservation and
    # its reminder, and the new block replaces the recorded intent.
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        accion,
        autor_id=autor.id,
        datos={"respuesta": respuesta, "motivo": (datos.motivo or "").strip() or None},
    )

    db.commit()
    db.refresh(reserva)
    db.refresh(fila)
    return reserva, fila
