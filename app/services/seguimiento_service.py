"""Live tracking of a service (RF-022 v1.0 delta).

Three answers the MVP could not give, all of them DERIVED:

* **``porcentaje_avance``** - where the service is along the cycle. The cycle
  is the main chain ``estado_service`` already derives from
  ``transicion_estado`` (principle P3), so a state inserted as data lengthens
  the bar by itself and no list of states is written down here. That is also
  what the AST guard in ``tests/test_operacion.py`` enforces.
* **``hora_estimada_entrega``** - recalculated on every state change instead of
  being the fixed ``fin`` the booking promised.
* **the delay notice of flow 4a** - when the recalculation lands more than
  fifteen minutes past what was promised, the customer is told the new time.

Nothing here runs inside a GET. The estimate is written where something
actually changed, so reading a reservation never notifies anybody.

HOW CA-01 IS MET, AND BY WHOM
-----------------------------
RF-022 ``CA-01`` reads: "dado un servicio en curso, cuando el estado cambia,
entonces el cliente ve el nuevo estado **en menos de 30 segundos**". That is a
latency budget, and this module spends none of it: the moment a state changes,
the new state, ``porcentaje_avance`` and ``hora_estimada_entrega`` are already
written and every read of the reservation returns them. The server's
contribution to the budget is one request.

**The remaining budget belongs to the mobile client, and the way it is spent
is POLLING at an interval of 30 seconds or less.** There is deliberately no
WebSocket and no SSE here: this is a student project with a simulated
infrastructure, a push channel would be a second delivery path to keep in sync
with ``notificacion_service``, and RF-022 asks for a latency, not for a
transport. ``GET /notificaciones`` (INC-5) and ``GET /reservas/{id}`` are the
two doors a client polls.

So CA-01 is verified **by Demonstration**, not by an automated test: no test in
this suite can observe the mobile client's timer, and one that faked it would
be measuring itself. It is the only acceptance criterion of the thirty-six
requirements verified this way, and saying so is the point - an unwritten
assumption is how a latency requirement quietly becomes nobody's. The
README repeats it under "RF-022".
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.horario import a_utc, ahora_utc, desde_bd
from app.models import EventoNotificacion, Reserva
from app.services import estado_service, notificacion_service

#: RF-022 flow 4a: past this much delay the customer is told the new time.
UMBRAL_RETRASO = timedelta(minutes=15)

#: A service that has not started yet shows no progress.
AVANCE_MINIMO = 0
AVANCE_MAXIMO = 100


def porcentaje_avance(db: Session, reserva: Reserva) -> int:
    """How far along the cycle this reservation is, as a percentage.

    The position comes from the main chain of ``transicion_estado``, never from
    a hand written list. A reservation sitting OUTSIDE that chain - a
    cancellation, or any exception branch a shop declares later - reports the
    progress it actually reached, read from its own history, instead of
    pretending it never started.
    """
    cadena = estado_service.cadena_principal(db)
    if len(cadena) < 2:
        return AVANCE_MINIMO

    posiciones = {estado: indice for indice, estado in enumerate(cadena)}
    indice = posiciones.get(reserva.estado)

    if indice is None:
        for fila in reversed(list(reserva.historial)):
            if fila.estado in posiciones:
                indice = posiciones[fila.estado]
                break

    if indice is None:
        return AVANCE_MINIMO
    return round(AVANCE_MAXIMO * indice / (len(cadena) - 1))


def estimar_entrega(db: Session, reserva: Reserva, momento: datetime | None = None) -> datetime:
    """When the vehicle is expected to be ready, as of ``momento`` (RF-022 step 4).

    Three inputs and no clock beyond ``ahora()``:

    * a finished service already knows its real end (``hora_fin_real``), and
      that is the answer - what is left is the hand-over, not the work;
    * otherwise the plan is "the duration of the service from when the vehicle
      came in", falling back to the booked start when it has not arrived yet;
    * the work still pending is the duration not yet covered by the progress,
      so a service that is behind pushes its own estimate forward.

    The answer is the later of the two, because a service cannot finish earlier
    than the work it still has left.
    """
    if reserva.hora_fin_real is not None:
        return desde_bd(reserva.hora_fin_real)

    momento = momento or ahora_utc()
    duracion = timedelta(minutes=reserva.servicio.duracion_min)
    base = desde_bd(reserva.hora_ingreso) if reserva.hora_ingreso else desde_bd(reserva.inicio)
    planificado = base + duracion

    restante = duracion * ((AVANCE_MAXIMO - porcentaje_avance(db, reserva)) / AVANCE_MAXIMO)
    return max(planificado, momento + restante)


def minutos_de_retraso(reserva: Reserva, estimado: datetime | None = None) -> int:
    """Minutes the estimate runs past what the booking promised, never negative."""
    referencia = estimado or desde_bd(reserva.hora_estimada_entrega) or desde_bd(reserva.fin)
    atraso = (referencia - desde_bd(reserva.fin)).total_seconds() / 60
    return max(0, int(atraso))


def actualizar_estimado(
    db: Session,
    reserva: Reserva,
    *,
    momento: datetime | None = None,
    **proveedores,
) -> datetime:
    """Recalculate the delivery time and warn if it slipped (RF-022 flow 4a).

    Called from every state change. The warning only goes out when the estimate
    MOVED and lands beyond the fifteen minute threshold, so walking a late
    service through four bay states tells the customer four different times
    rather than the same one four times - and a service running on time says
    nothing at all.
    """
    momento = momento or ahora_utc()
    anterior = desde_bd(reserva.hora_estimada_entrega)
    estimado = estimar_entrega(db, reserva, momento)
    reserva.hora_estimada_entrega = a_utc(estimado)

    retraso = minutos_de_retraso(reserva, estimado)
    if retraso > UMBRAL_RETRASO.total_seconds() / 60 and estimado != anterior:
        notificacion_service.despachar(
            db,
            reserva.usuario,
            EventoNotificacion.RETRASO.value,
            reserva=reserva,
            datos={"minutos_retraso": retraso},
            momento=momento,
            **proveedores,
        )

    return estimado
