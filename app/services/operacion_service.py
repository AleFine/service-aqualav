"""Operation of a reservation: state machine, check-in and check-out.

RF-021 is the requirement the whole extension story hangs from. The allowed
moves are READ FROM ``transicion_estado`` on every attempt (EXTENSION POINT
P3): growing to the eleven states of v1.0 is a data migration, not a code
change, and nothing in this module may enumerate the transitions.

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
from app.models import EstadoReserva, Reserva, TransicionEstado, Usuario
from app.repositories import pago as pago_repo
from app.repositories import reserva as reserva_repo
from app.repositories import transicion as transicion_repo
from app.schemas import CheckInIn, CheckOutIn
from app.services import eventos
from app.services.notificador import NOTIFICADOR_PREDETERMINADO, Notificador

#: RF-019 flow 3a: past this delay the check-in needs an explicit confirmation.
TOLERANCIA_RETRASO = timedelta(minutes=20)

#: ``origen_llamada`` of the generic ``POST /reservas/{id}/estado``. It carries
#: no invariant of its own, so it may only perform moves declared with no owner.
ENDPOINT_GENERICO = "estado"


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
) -> Reserva:
    """Move a reservation to ``destino``, validating against the table.

    ``origen_llamada`` names the operation performing the move; it defaults to
    the generic endpoint. See :func:`validar_transicion` for what is checked.
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

    if confirmar:
        db.commit()
        db.refresh(reserva)

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
) -> list[Reserva]:
    """Find today's and upcoming non terminal reservations (RF-019 step 1)."""
    codigo_limpio = (codigo or "").strip().upper() or None
    placa_limpia = "".join((placa or "").split()).upper() or None

    if codigo_limpio is None and placa_limpia is None:
        raise RecursoNoEncontrado("Indica un código de reserva o una placa para buscar.")

    inicio_del_dia = a_lima(ahora()).replace(hour=0, minute=0, second=0, microsecond=0)
    encontradas = reserva_repo.buscar_activas(
        db,
        transicion_repo.listar_estados_no_terminales(db),
        codigo=codigo_limpio,
        placa=placa_limpia,
        desde=inicio_del_dia,
    )
    if not encontradas:
        raise RecursoNoEncontrado(
            "No encontramos una reserva activa con esos datos. Verifica el código o la placa."
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
) -> Reserva:
    """Register the vehicle entering the shop (RF-019).

    Rejects a reservation that was already attended or cancelled (CA-02) and
    asks for an explicit confirmation when the customer is more than twenty
    minutes late (flow 3a).

    LIMITATION: the DESTINATION is still fixed here, because it is what "check
    in" means. If v0.4 replaces ``en_atencion`` by ``en_lavado/secado/acabado``
    as Annex A plans, this line has to be revisited. The ORIGIN is not fixed:
    which states may be checked in from is read from ``transicion_estado``.
    """
    # Validated before the delay check so an already attended reservation gets
    # 422 TRANSICION_INVALIDA (CA-02) rather than the delay confirmation.
    validar_transicion(
        db,
        reserva,
        EstadoReserva.EN_ATENCION.value,
        permisos,
        origen_llamada="check_in",
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
        EstadoReserva.EN_ATENCION.value,
        autor,
        permisos,
        origen_llamada="check_in",
        accion=eventos.RESERVA_CHECK_IN,
        datos={"minutos_retraso": retraso, "confirmar_retraso": bool(datos.confirmar_retraso)},
        notificador=notificador,
    )


def check_out(
    db: Session,
    reserva: Reserva,
    datos: CheckOutIn,
    autor: Usuario,
    permisos: list[str],
    *,
    notificador: Notificador = NOTIFICADOR_PREDETERMINADO,
) -> Reserva:
    """Hand the vehicle back to the customer (RF-024).

    RN-09: the service must be finished AND the payment confirmed. A pending
    payment blocks the delivery with 422 ``PAGO_PENDIENTE`` (CA-01).

    The same LIMITATION as :func:`check_in` applies to the destination.
    """
    # "Finished" is the declared move into the delivery state, not a state
    # literal: the transition table says which origins may be delivered from.
    # Validated first so a reservation that is not ready to be handed back is
    # refused with 422 TRANSICION_INVALIDA instead of PAGO_PENDIENTE.
    validar_transicion(
        db,
        reserva,
        EstadoReserva.ENTREGADO.value,
        permisos,
        origen_llamada="check_out",
    )

    if pago_repo.obtener_confirmado(db, reserva.id) is None:
        raise PagoPendiente(
            detalles=[detalle("pago", "Registra el cobro del servicio antes de entregar.")]
        )

    reserva.hora_entrega = ahora_utc()
    reserva.conformidad_cliente = bool(datos.conformidad_cliente)

    return cambiar_estado(
        db,
        reserva,
        EstadoReserva.ENTREGADO.value,
        autor,
        permisos,
        origen_llamada="check_out",
        accion=eventos.RESERVA_CHECK_OUT,
        datos={"conformidad_cliente": bool(datos.conformidad_cliente)},
        notificador=notificador,
    )
