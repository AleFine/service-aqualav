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

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.errors import RecursoNoEncontrado, detalle
from app.core.horario import a_lima, ahora_utc, desde_bd
from app.models import (
    VENTANA_RECORDATORIO,
    EstadoRecordatorio,
    EventoNotificacion,
    Recordatorio,
    Reserva,
    RespuestaRecordatorio,
    Usuario,
)
from app.repositories import notificacion as notificacion_repo
from app.schemas import RecordatorioRespuestaIn, ReprogramacionIn
from app.services import eventos, notificacion_service, operacion_service, reserva_service

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

    "Already went out" is ``enviado_en``, not "the row exists". The two used to
    be the same thing; since RF-015 they are not, because rescheduling RE-ARMS
    the reminder of the block it moved (``rearmar_recordatorio`` clears
    ``enviado_en``) and the customer is entitled to be reminded of the
    appointment they actually have now. The sweep stays idempotent either way:
    running it twice in the same window still finds ``enviado_en`` set.
    """
    existente = notificacion_repo.obtener_recordatorio(db, reserva.id)
    if existente is not None and existente.enviado_en is not None:
        return None
    if not corresponde_recordar(db, reserva):
        return None

    momento = momento or ahora_utc()
    programado_para = desde_bd(reserva.inicio) - VENTANA_RECORDATORIO
    if existente is not None:
        fila = notificacion_repo.marcar_recordatorio_enviado(
            db,
            existente,
            programado_para=programado_para,
            enviado_en=momento,
            estado=EstadoRecordatorio.ENVIADO.value,
        )
    else:
        fila = notificacion_repo.crear_recordatorio(
            db,
            reserva_id=reserva.id,
            programado_para=programado_para,
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
    * ``reprogramo`` - the REAL move, since INC-7, when the answer carries the
      block the customer chose (``nuevo_inicio``). It is
      :func:`app.services.reserva_service.reprogramar` and nothing else, so
      RN-06 and the two-hour window of RF-015 flow 2b are enforced in one
      place and this one has no opinion about either. A refusal comes back as
      its own 422 and the reminder keeps the answer that was given.

      Without a block it stays what INC-5 left: a RECORDED INTENT. The
      reminder's three buttons are a notification, not a date picker - the app
      opens one when the customer taps "reprogramar" - so an answer that names
      no block is the customer saying "I want to move this", and the event is
      what lets the counter phone them back if they never finish.

      Worth knowing, because it is the requirements talking to each other and
      not a bug: the reminder goes out when the booking starts within two hours
      (RF-030), and RF-015 flow 2b demands MORE than two hours. A customer who
      answers the reminder promptly is therefore usually past the rescheduling
      window and gets 422 ``REPROGRAMACION_FUERA_DE_PLAZO`` - whose message
      points at the other button the same reminder offers, cancelling. Relaxing
      the window "because it came from a reminder" would be exactly the second
      copy of the rule this function refuses to keep.
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

    if respuesta == RespuestaRecordatorio.REPROGRAMO.value and datos.nuevo_inicio is not None:
        # RF-015 for real. ``reprogramar`` owns the transaction, writes its own
        # ``reserva.reprogramada`` event and re-arms this very reminder for the
        # new block, so nothing is committed twice and the intent event is not
        # written for a move that actually happened.
        reserva = reserva_service.reprogramar(
            db, reserva, ReprogramacionIn(inicio=datos.nuevo_inicio), autor, permisos
        )
        db.refresh(fila)
        return reserva, fila

    accion = (
        eventos.RESERVA_ASISTENCIA_CONFIRMADA
        if respuesta == RespuestaRecordatorio.CONFIRMO.value
        else eventos.RESERVA_REPROGRAMACION_SOLICITADA
    )
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
