"""Operation of a reservation: state machine, check-in and check-out.

RF-021 is the requirement the whole extension story hangs from. The allowed
moves are READ FROM ``transicion_estado`` on every attempt (EXTENSION POINT
P3): growing to the eleven states of v1.0 WAS a data migration
(``0003_estados_roles_v1``) and not a code change, and nothing in this module
may enumerate the transitions.

Both ends of every move are data. An operation such as the check-in knows its
own NAME (the value stored in ``transicion_estado.endpoint``) and asks the
table where that name leads from the current state, so a state that is renamed,
split or inserted never reaches this file.

Every state change - including the ones check-in and check-out perform - goes
through :func:`cambiar_estado`, so the history row (P7) and the domain event
are written in exactly one place.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import (
    PagoPendiente,
    PermisoDenegado,
    RecursoNoEncontrado,
    RetrasoRequiereConfirmacion,
    TransicionInvalida,
    detalle,
)
from app.core.horario import a_lima, ahora, ahora_utc, desde_bd
from app.models import EstadoReserva, EventoNotificacion, Reserva, TransicionEstado, Usuario
from app.repositories import pago as pago_repo
from app.repositories import reserva as reserva_repo
from app.repositories import transicion as transicion_repo
from app.schemas import CheckInIn, CheckOutIn, RevisionIn
from app.services import bahia_service, eventos, notificacion_service, seguimiento_service
from app.services.notificador import NOTIFICADOR_PREDETERMINADO, Notificador

#: RF-019 flow 3a: past this delay the check-in needs an explicit confirmation.
TOLERANCIA_RETRASO = timedelta(minutes=20)

#: ``origen_llamada`` of the generic ``POST /reservas/{id}/estado``. It carries
#: no invariant of its own, so it may only perform moves declared with no owner.
ENDPOINT_GENERICO = "estado"

#: Names of the operations that OWN a move. They are the value stored in
#: ``transicion_estado.endpoint``, and they are the only thing these operations
#: know about the state machine: the destination of each one is looked up in the
#: table (see :func:`destino_declarado`), never written down here.
ENDPOINT_CHECK_IN = "check_in"
ENDPOINT_CHECK_OUT = "check_out"
ENDPOINT_CANCELACION = "cancelacion"
#: RF-020: bay and operator assignment.
ENDPOINT_ASIGNACION = "asignacion"
#: RF-024 flow 3a: the customer objected to the result. It owns its own move so
#: the check-out never has two destinations to choose from.
ENDPOINT_REVISION = "revision"
#: RF-026: the online charge. It owns the move out of "waiting for the money",
#: which is why no generic caller can confirm a reservation that was never
#: paid. INC-4 implements the operation; INC-1A already wrote the row.
ENDPOINT_PAGO = "pago"


def transiciones_permitidas(db: Session, reserva: Reserva, permisos: list[str]) -> list[str]:
    """States this reservation can move to, for this caller.

    Read from ``transicion_estado`` and filtered by the caller's permissions:
    the mobile app renders its action buttons from this list, so it does not
    hardcode the state machine either.
    """
    codigos = set(permisos or ())
    return [
        transicion.estado_destino
        for transicion in transicion_repo.listar_por_origen(db, reserva.estado)
        if transicion.permiso_requerido in codigos
    ]


def _normalizar(destino: str) -> str:
    return destino.value if isinstance(destino, EstadoReserva) else str(destino)


def tiene_operacion_declarada(db: Session, reserva: Reserva, endpoint: str) -> bool:
    """Whether ``endpoint`` still owns a move out of the current state.

    Data-driven "is this operation applicable?": the waiting queue of RF-020
    uses it to drop the rows of reservations that already left the counter,
    without naming a single state.
    """
    return any(
        transicion.endpoint == endpoint
        for transicion in transicion_repo.listar_por_origen(db, reserva.estado)
    )


def es_terminal(db: Session, estado: str) -> bool:
    """Whether no declared move leaves ``estado`` (P3).

    The ONLY definition of "the service is over" in the code base. It is what
    tells :func:`cambiar_estado` to give the bay back, so the delivery of
    RF-024 and the cancellation of RF-016 release it through the same line and
    a state added as data behaves correctly with no change here.
    """
    return estado not in transicion_repo.listar_estados_no_terminales(db)


def destino_declarado(db: Session, reserva: Reserva, endpoint: str) -> str:
    """The state ``endpoint`` moves this reservation to, read from the table.

    EXTENSION POINT P3, the half the MVP was still missing. The ORIGIN of every
    move was already data, but the DESTINATION was a literal inside each
    operation (``check_in`` wrote ``en_atencion``, ``check_out`` wrote
    ``entregado``), so Annex A v1.0 could not rename or split a state without
    editing the service layer. Now both ends live in ``transicion_estado``:
    check-in lands on ``en_recepcion`` because the row says so, and the
    check-out of a reservation coming back from ``en_revision`` needs no branch.

    Raises ``TransicionInvalida`` (422) when the table declares no move owned by
    this operation leaving the current state - which is what answers the
    check-in of an already attended reservation (RF-019 CA-02) and the check-out
    of a service that is not finished yet (RF-024).
    """
    candidatas = [
        transicion
        for transicion in transicion_repo.listar_por_origen(db, reserva.estado)
        if transicion.endpoint == endpoint
    ]

    if not candidatas:
        raise TransicionInvalida(
            detalles=[
                detalle(
                    "estado",
                    f"La reserva está en «{reserva.estado}» y esta operación no aplica "
                    "a ese estado.",
                )
            ]
        )

    if len(candidatas) > 1:
        # The table is data and may be edited: refuse to guess rather than pick
        # one destination and silently skip the other.
        destinos = ", ".join(sorted(transicion.estado_destino for transicion in candidatas))
        raise TransicionInvalida(
            "Esa operación tiene más de un destino declarado para el estado actual, "
            "así que no puede decidir por sí sola. Revisa la tabla de transiciones.",
            detalles=[detalle("estado", f"Destinos declarados: {destinos}.")],
        )

    return candidatas[0].estado_destino


def validar_transicion(
    db: Session,
    reserva: Reserva,
    destino: str,
    permisos: list[str],
    *,
    origen_llamada: str = ENDPOINT_GENERICO,
) -> TransicionEstado:
    """The row declaring ``reserva.estado -> destino``, checked for this caller.

    Three checks in this order, so the reason a caller gets back is the most
    specific one:

    1. the move must be declared in ``transicion_estado`` (422, RF-021 flow 4a);
    2. the caller must hold the permission that row demands (403);
    3. the row must belong to the operation that is asking (422).

    The third one is what closes the alternate routes: a transition whose own
    endpoint carries invariants and side effects (the RN-09 payment check, the
    cancellation reason, the entry and delivery timestamps) is refused to every
    other endpoint, including the generic ``POST /estado``. The ownership is
    DATA, not a list of states in this module, so a state inserted in v0.4
    brings its own rule with it (principle P3).
    """
    destino = _normalizar(destino)
    origen = reserva.estado

    transicion = transicion_repo.obtener(db, origen, destino)
    if transicion is None:
        validos = transiciones_permitidas(db, reserva, permisos)
        raise TransicionInvalida(
            detalles=[
                detalle("estado", f"La reserva está en «{origen}» y no puede pasar a «{destino}»."),
                detalle("estados_validos", ", ".join(validos) if validos else "ninguno"),
            ]
        )

    if transicion.permiso_requerido not in set(permisos or ()):
        raise PermisoDenegado(
            detalles=[detalle("estado", f"Se requiere el permiso {transicion.permiso_requerido}.")]
        )

    if transicion.endpoint and transicion.endpoint != origen_llamada:
        raise TransicionInvalida(
            "Esa transición tiene su propia operación, que valida condiciones "
            "que este endpoint no puede comprobar.",
            detalles=[
                detalle(
                    "estado",
                    f"Usa la operación «{transicion.endpoint}» para pasar a «{destino}».",
                )
            ],
        )

    return transicion


def cambiar_estado(
    db: Session,
    reserva: Reserva,
    destino: str,
    autor: Usuario,
    permisos: list[str],
    *,
    origen_llamada: str = ENDPOINT_GENERICO,
    accion: str = eventos.RESERVA_ESTADO_CAMBIADO,
    datos: dict[str, Any] | None = None,
    notificador: Notificador = NOTIFICADOR_PREDETERMINADO,
    confirmar: bool = True,
    **proveedores,
) -> Reserva:
    """Move a reservation to ``destino``, validating against the table.

    ``origen_llamada`` names the operation performing the move; it defaults to
    the generic endpoint. See :func:`validar_transicion` for what is checked.

    ``proveedores`` is forwarded verbatim to ``notificacion_service.despachar``
    (``correo``, ``push``, ``en_app``, ``espera``) so a test can make a channel
    fail without every caller in between having to know about it.
    """
    destino = _normalizar(destino)
    origen = reserva.estado

    transicion = validar_transicion(db, reserva, destino, permisos, origen_llamada=origen_llamada)

    momento = ahora_utc()
    reserva.estado = destino
    if transicion.marca_fin_servicio:
        # RF-022 CA-02: the customer sees the real finishing time. Which move
        # ends the service is declared in the table, never listed here (P3).
        reserva.hora_fin_real = momento

    if es_terminal(db, destino):
        # RF-024 step 4 / RF-016: a finished or cancelled service stops holding
        # the bay and leaves the waiting queue. Terminality is read from the
        # table, so this is not a list of states in disguise.
        bahia_service.liberar_recursos(db, reserva)

    reserva_repo.agregar_historial(
        db,
        reserva_id=reserva.id,
        estado=destino,
        autor_id=autor.id if autor else None,
        ocurrido_en=momento,
    )

    cuerpo_evento: dict[str, Any] = {"estado_origen": origen, "estado_destino": destino}
    cuerpo_evento.update(datos or {})
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        accion,
        autor_id=autor.id if autor else None,
        datos=cuerpo_evento,
    )

    # RF-022 step 4: the delivery time is recalculated HERE, where something
    # actually changed, and warns the customer when it slips past fifteen
    # minutes (flow 4a). Before the lifecycle notice so the message that goes
    # out already carries the new time.
    seguimiento_service.actualizar_estimado(db, reserva, momento=momento, **proveedores)

    # RF-029: which lifecycle event this move raises is declared in the table
    # (``transicion_estado.evento_notificacion``), never decided here. A move
    # that declares none is internal and only reaches the in-app feed.
    evento = transicion.evento_notificacion or EventoNotificacion.ESTADO_CAMBIADO.value
    notificacion_service.despachar(
        db,
        reserva.usuario,
        evento,
        reserva=reserva,
        datos={"estado_origen": origen, "estado_destino": destino},
        momento=momento,
        **proveedores,
    )

    if confirmar:
        db.commit()
        db.refresh(reserva)

    # P8 is still honoured: a caller may inject its own notifier and it is
    # still told, in addition to the persisted channels above.
    if notificador is not NOTIFICADOR_PREDETERMINADO:
        notificador.notificar(
            reserva.usuario_id,
            "Tu reserva cambió de estado",
            f"La reserva {reserva.codigo} ahora está en estado «{destino}».",
            {"reserva_id": reserva.id, "estado": destino},
        )
    return reserva


def buscar(
    db: Session,
    *,
    codigo: str | None = None,
    placa: str | None = None,
    qr: str | None = None,
) -> list[Reserva]:
    """Find today's and upcoming non terminal reservations (RF-019 step 1).

    v1.0 adds the third entry point: the QR printed on the reception ticket.
    Scanning it is the same query by another key, so nothing else about the
    check-in changes (RF-019 delta).
    """
    codigo_limpio = (codigo or "").strip().upper() or None
    placa_limpia = "".join((placa or "").split()).upper() or None
    qr_limpio = "".join((qr or "").split()).upper() or None

    if codigo_limpio is None and placa_limpia is None and qr_limpio is None:
        raise RecursoNoEncontrado(
            "Indica un código de reserva, una placa o un código QR para buscar."
        )

    inicio_del_dia = a_lima(ahora()).replace(hour=0, minute=0, second=0, microsecond=0)
    encontradas = reserva_repo.buscar_activas(
        db,
        transicion_repo.listar_estados_no_terminales(db),
        codigo=codigo_limpio,
        placa=placa_limpia,
        codigo_qr=qr_limpio,
        desde=inicio_del_dia,
    )
    if not encontradas:
        raise RecursoNoEncontrado(
            "No encontramos una reserva activa con esos datos. "
            "Verifica el código, la placa o vuelve a escanear el QR."
        )
    return encontradas


def minutos_de_retraso(reserva: Reserva, momento: datetime | None = None) -> int:
    """Minutes the customer is late, never negative."""
    referencia = momento or ahora()
    inicio = desde_bd(reserva.inicio)
    atraso = (a_lima(referencia) - a_lima(inicio)).total_seconds() / 60
    return max(0, int(atraso))


def check_in(
    db: Session,
    reserva: Reserva,
    datos: CheckInIn,
    autor: Usuario,
    permisos: list[str],
    *,
    notificador: Notificador = NOTIFICADOR_PREDETERMINADO,
    **proveedores,
) -> Reserva:
    """Register the vehicle entering the shop (RF-019).

    Rejects a reservation that was already attended or cancelled (CA-02) and
    asks for an explicit confirmation when the customer is more than twenty
    minutes late (flow 3a).

    Neither end of the move is written here any more: the table says which
    states may be checked in from AND where the check-in lands (P3). That is
    what let v1.0 replace ``en_atencion`` by ``en_recepcion`` as a data
    migration, with no change in this function.
    """
    destino = destino_declarado(db, reserva, ENDPOINT_CHECK_IN)

    # Validated before the delay check so an already attended reservation gets
    # 422 TRANSICION_INVALIDA (CA-02) rather than the delay confirmation.
    validar_transicion(
        db,
        reserva,
        destino,
        permisos,
        origen_llamada=ENDPOINT_CHECK_IN,
    )

    retraso = minutos_de_retraso(reserva)
    if retraso > TOLERANCIA_RETRASO.total_seconds() / 60 and not datos.confirmar_retraso:
        raise RetrasoRequiereConfirmacion(
            detalles=[
                detalle("confirmar_retraso", f"El cliente llegó {retraso} minutos tarde."),
                detalle("minutos_retraso", str(retraso)),
            ]
        )

    reserva.hora_ingreso = ahora_utc()
    reserva.observaciones_ingreso = (datos.observaciones or "").strip() or None

    return cambiar_estado(
        db,
        reserva,
        destino,
        autor,
        permisos,
        origen_llamada=ENDPOINT_CHECK_IN,
        accion=eventos.RESERVA_CHECK_IN,
        datos={"minutos_retraso": retraso, "confirmar_retraso": bool(datos.confirmar_retraso)},
        notificador=notificador,
        **proveedores,
    )


def check_out(
    db: Session,
    reserva: Reserva,
    datos: CheckOutIn,
    autor: Usuario,
    permisos: list[str],
    *,
    notificador: Notificador = NOTIFICADOR_PREDETERMINADO,
    **proveedores,
) -> Reserva:
    """Hand the vehicle back to the customer (RF-024).

    RN-09: the service must be finished AND the payment confirmed. A pending
    payment blocks the delivery with 422 ``PAGO_PENDIENTE`` (CA-01).

    Like the check-in, both ends of the move are data: v1.0 added a second
    origin (``en_revision``, RF-024 flow 3a) as a row, not as a branch.
    """
    # "Ready to be handed back" is the declared move out of the current state,
    # not a state literal. Validated first so a reservation that is not ready
    # is refused with 422 TRANSICION_INVALIDA instead of PAGO_PENDIENTE.
    destino = destino_declarado(db, reserva, ENDPOINT_CHECK_OUT)

    validar_transicion(
        db,
        reserva,
        destino,
        permisos,
        origen_llamada=ENDPOINT_CHECK_OUT,
    )

    if pago_repo.obtener_confirmado(db, reserva.id) is None:
        raise PagoPendiente(
            detalles=[detalle("pago", "Registra el cobro del servicio antes de entregar.")]
        )

    reserva.hora_entrega = ahora_utc()
    reserva.conformidad_cliente = bool(datos.conformidad_cliente)

    # RF-024 step 4: the delivery opens the rating window. HOOK, on purpose -
    # ``calificacion`` is INC-6 (RF-031) and back-filling the moment the window
    # opened would be impossible, so the event is written now and RN-10 will
    # count its seven calendar days from here.
    # TODO(INC-6, RF-031): read this event to expose "puedes calificar" and to
    # close the window seven days later. The call site must not change.
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        eventos.RESERVA_CALIFICACION_HABILITADA,
        autor_id=autor.id if autor else None,
        datos={"habilitada_en": reserva.hora_entrega.isoformat()},
    )

    return cambiar_estado(
        db,
        reserva,
        destino,
        autor,
        permisos,
        origen_llamada=ENDPOINT_CHECK_OUT,
        accion=eventos.RESERVA_CHECK_OUT,
        datos={"conformidad_cliente": bool(datos.conformidad_cliente)},
        notificador=notificador,
        **proveedores,
    )


def enviar_a_revision(
    db: Session,
    reserva: Reserva,
    datos: RevisionIn,
    autor: Usuario,
    permisos: list[str],
    *,
    notificador: Notificador = NOTIFICADOR_PREDETERMINADO,
    **proveedores,
) -> Reserva:
    """Register what the customer objected to and send the service back (RF-024 3a).

    Annex A v1.0 gives ``finalizado`` TWO exits - the delivery and the review -
    and INC-1A gave each one its own owning endpoint precisely so neither has
    to guess. This one keeps the observation, which is what the operator reads
    before reworking the vehicle (``en_revision -> acabado``).

    The bay is NOT released: the vehicle never left, and ``en_revision`` has
    outgoing moves, so it is not terminal.
    """
    destino = destino_declarado(db, reserva, ENDPOINT_REVISION)

    validar_transicion(
        db,
        reserva,
        destino,
        permisos,
        origen_llamada=ENDPOINT_REVISION,
    )

    reserva.observacion_revision = datos.observacion.strip()
    reserva.conformidad_cliente = False

    return cambiar_estado(
        db,
        reserva,
        destino,
        autor,
        permisos,
        origen_llamada=ENDPOINT_REVISION,
        accion=eventos.RESERVA_EN_REVISION,
        datos={"observacion": reserva.observacion_revision},
        notificador=notificador,
        **proveedores,
    )
