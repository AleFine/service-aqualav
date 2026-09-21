"""Reservation lifecycle: creation, listing, detail, rescheduling, cancellation.

RF-014, RF-015, RF-016 and RF-017. The concurrency-sensitive part is
:func:`crear`: the bay set is locked for the duration of the transaction, so
two simultaneous confirmations over the same block produce exactly one 201 and
one 409 (CA-02). :func:`reprogramar` locks the same set for the same reason.

**RN-06 lives here and nowhere else.** The reminder of RF-030 offers the
customer a "reprogramo" button, but the answer to that button ends up calling
:func:`reprogramar` like everybody else: a rule enforced in two places is a
rule that will disagree with itself the first time one of them is edited.
"""

from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.core.codigos import generar_codigo_qr, generar_codigo_reserva
from app.core.errors import (
    DatosInvalidos,
    LimiteDeReprogramaciones,
    PermisoDenegado,
    RecursoNoEncontrado,
    ReprogramacionFueraDePlazo,
    ReservaAnticipacionInsuficiente,
    ReservaBloqueOcupado,
    ReservaFueraDeHorario,
    TransicionInvalida,
    VehiculoNoVerificado,
    detalle,
)
from app.core.horario import (
    ZONA_LIMA,
    a_lima,
    a_utc,
    ahora,
    ahora_utc,
    dentro_de_horario,
    desde_bd,
)
from app.models import (
    VENTANA_RECORDATORIO,
    EstadoRecordatorio,
    EstadoReserva,
    EventoNotificacion,
    ModalidadPago,
    Reserva,
    Servicio,
    TipoReembolso,
    Usuario,
)
from app.repositories import bahia as bahia_repo
from app.repositories import notificacion as notificacion_repo
from app.repositories import pago as pago_repo
from app.repositories import reserva as reserva_repo
from app.repositories import transicion as transicion_repo
from app.repositories import vehiculo as vehiculo_repo
from app.schemas import (
    TAMANIO_PAGINA_DEFECTO,
    TAMANIO_PAGINA_MAXIMO,
    AtencionInmediataIn,
    CheckInIn,
    Dinero,
    ReprogramacionIn,
    ReservaCrear,
)
from app.services import (
    agenda_service,
    eventos,
    fidelizacion_service,
    notificacion_service,
    operacion_service,
    pago_service,
    reembolso_service,
    servicio_service,
    tarifa_service,
)
from app.services.disponibilidad_service import ANTICIPACION_MINIMA, bloques_cercanos
from app.services.notificador import NOTIFICADOR_PREDETERMINADO, Notificador
from app.services.politica_cancelacion import POLITICA_PREDETERMINADA, PoliticaCancelacion

#: Permission that lifts the "only your own reservations" filter (RF-017 CA-03).
PERMISO_LEER_TODAS = "reserva:leer_todas"

#: Permission RF-015 demands (P5). Checked in the SERVICE and not only in the
#: router, because ``reprogramar`` has a second door: the answer to the reminder
#: of RF-030, whose endpoint is guarded by the READING permissions. Without this
#: line an operator - who holds ``reserva:leer_todas`` so the bay screens work -
#: could move somebody's appointment by answering their reminder.
PERMISO_REPROGRAMAR = "reserva:reprogramar"

#: How many times to retry on a reservation code collision before giving up.
INTENTOS_CODIGO = 5

#: RF-016 flow 5a / RN-05: the reason written on the refund a cancellation
#: starts. It is user facing, because the customer sees it in their history.
MOTIVO_REEMBOLSO_CANCELACION = "Cancelación de la reserva {codigo} (RN-05)."

#: RN-06: "una reserva puede reprogramarse COMO MÁXIMO DOS VECES; luego debe
#: cancelarse y crearse nuevamente". This is the single declaration of the rule
#: in the code base.
MAXIMO_REPROGRAMACIONES = 2

#: RF-015 flow 2b: "se exige que falten MÁS DE DOS HORAS para el inicio". The
#: threshold is strict, the same reading RN-05 gets in ``politica_cancelacion``:
#: two hours exactly is already late, and a block freed with less notice than
#: that is one the shop cannot resell.
ANTICIPACION_REPROGRAMACION = timedelta(hours=2)

#: How many states a multi-state history filter may name at once (RF-017 v1.0).
#: Not a business rule - a guard so a crafted query cannot build an unbounded
#: ``IN``. The machine has eleven states, so this is room to spare.
MAXIMO_ESTADOS_FILTRO = 20


def _generar_codigo(db: Session) -> str:
    """A unique ``AQL-XXXXXX`` code. Collisions are astronomically unlikely."""
    for _ in range(INTENTOS_CODIGO):
        codigo = generar_codigo_reserva()
        if not reserva_repo.existe_codigo(db, codigo):
            return codigo
    return generar_codigo_reserva()  # pragma: no cover - practically unreachable


def _generar_qr(db: Session) -> str:
    """A unique token for the reception ticket's QR (RF-019 v1.0)."""
    for _ in range(INTENTOS_CODIGO):
        codigo = generar_codigo_qr()
        if not reserva_repo.existe_codigo_qr(db, codigo):
            return codigo
    return generar_codigo_qr()  # pragma: no cover - practically unreachable


def puede_ver_todas(permisos: list[str]) -> bool:
    return PERMISO_LEER_TODAS in set(permisos or ())


def _exigir_anticipacion(inicio_lima: datetime) -> None:
    """RN-02: a block has to be at least an hour away (RF-013 CA-02)."""
    if inicio_lima < a_lima(ahora()) + ANTICIPACION_MINIMA:
        raise ReservaAnticipacionInsuficiente(
            detalles=[detalle("inicio", "Elige un bloque con al menos 60 minutos de anticipación.")]
        )


def _exigir_horario(
    db: Session,
    servicio: Servicio,
    inicio_lima: datetime,
    fin_lima: datetime,
    *,
    alternativa: str,
) -> None:
    """RN-07 + RF-018: the whole block has to fit inside an OPEN day.

    RN-07 is DATA (RF-018): the calendar carries the opening hours of the week
    plus the holidays and the shop-wide closures declared for that date, so a
    booking on a holiday is refused with the same error as one at midnight -
    and so is a RESCHEDULE onto one (RF-013 delta, verified by its own test).
    """
    calendario = agenda_service.calendario(db, inicio_lima.date(), fin_lima.date())
    if dentro_de_horario(inicio_lima, fin_lima, calendario):
        return
    motivo = calendario.motivo_no_laborable(inicio_lima.date())
    raise ReservaFueraDeHorario(detalles=[detalle("inicio", motivo or alternativa)])


def crear(
    db: Session,
    usuario: Usuario,
    datos: ReservaCrear,
    *,
    notificador: Notificador = NOTIFICADOR_PREDETERMINADO,
    **proveedores,
) -> Reserva:
    """Confirm a reservation (RF-014).

    Order of the checks: the vehicle must belong to the caller (RN-01), the
    block must be at least an hour away (RN-02) and fully inside the opening
    hours (RN-07). Only then is the bay set locked and a free bay picked.
    """
    servicio = servicio_service.obtener_publico(db, datos.servicio_id)
    precio = servicio_service.precio_vigente(servicio)

    vehiculo = vehiculo_repo.obtener_por_id(db, datos.vehiculo_id)
    if vehiculo is None or vehiculo.usuario_id != usuario.id:
        # RN-01. A 404 rather than a 403: never confirm that someone else's
        # vehicle id exists.
        raise RecursoNoEncontrado(
            "No encontramos ese vehículo en tu cuenta. Regístralo antes de reservar.",
            detalles=[detalle("vehiculo_id", "El vehículo no pertenece a tu cuenta.")],
        )

    if not vehiculo.activo:
        # RF-008: the deletion is logical, so the vehicle is still readable in
        # the history (CA-02) and still refuses to take a NEW booking.
        raise RecursoNoEncontrado(
            "Ese vehículo está dado de baja. Regístralo otra vez para reservar con él.",
            detalles=[detalle("vehiculo_id", "El vehículo está dado de baja.")],
        )

    if settings.exigir_vehiculo_verificado and not vehiculo.verificado:
        # RN-01 v1.0 reads "registered AND VERIFIED". The rule is implemented
        # here and shipped OFF (see the setting): no requirement says how a
        # vehicle gets verified before its first visit, and demanding it would
        # mean a new customer cannot book the appointment that would let the
        # counter verify their car.
        raise VehiculoNoVerificado(
            detalles=[detalle("vehiculo_id", "Recepción debe verificar el vehículo.")]
        )

    inicio_lima = a_lima(datos.inicio)
    fin_lima = inicio_lima + timedelta(minutes=servicio.duracion_min)

    _exigir_anticipacion(inicio_lima)
    _exigir_horario(
        db,
        servicio,
        inicio_lima,
        fin_lima,
        alternativa=(
            f"El servicio dura {servicio.duracion_min} minutos y debe terminar antes del cierre."
        ),
    )

    inicio_utc = a_utc(inicio_lima)
    fin_utc = a_utc(fin_lima)

    # RF-012 / RN-04: the tariff is computed BEFORE the bay set is locked, so a
    # request rejected for an unknown add-on never holds the lock. The day the
    # SERVICE happens is what decides which promotions are in force, not the
    # day it is booked.
    desglose = tarifa_service.calcular(
        db,
        servicio_id=servicio.id,
        precio_base_centimos=precio.monto_centimos,
        moneda=precio.moneda,
        tipo_vehiculo=vehiculo.tipo,
        adicionales_ids=datos.adicionales,
        cupon=datos.cupon,
        fecha=inicio_lima.date(),
        # RF-032: a redeemed coupon belongs to ONE customer, so the engine has
        # to know whose booking this is before it honours one.
        usuario_id=usuario.id,
    )

    # Serialization point: every concurrent creation queues on this lock, so
    # the overlap check below cannot be raced (RF-014 CA-02).
    bahias = bahia_repo.listar_activas_bloqueadas(db)
    ocupadas = reserva_repo.bahias_ocupadas(
        db, inicio_utc, fin_utc, transicion_repo.listar_estados_no_terminales(db)
    )
    # RF-018 CA-01: a blocked bay is not on offer, so a slot the agenda took
    # out never comes back through the booking door either.
    franjas = agenda_service.franjas_bloqueadas(db, inicio_lima.date(), fin_lima.date())
    ocupadas = ocupadas | agenda_service.bahias_bloqueadas(
        franjas, [bahia.id for bahia in bahias], inicio_utc, fin_utc
    )
    libre = next((bahia for bahia in bahias if bahia.id not in ocupadas), None)

    if libre is None:
        cercanos = bloques_cercanos(db, servicio, inicio_lima)
        raise ReservaBloqueOcupado(
            detalles=[
                detalle("inicio", f"Bloque libre cercano: {bloque.inicio.isoformat()}.")
                for bloque in cercanos
            ]
            or [detalle("inicio", "No quedan bloques libres ese día. Prueba con otra fecha.")]
        )

    # RF-014 flow 2a + RF-025: the modality decides where the booking is BORN.
    # This is the one edge of Annex A that is not a row in
    # ``transicion_estado`` - there is no origin state to look the move up by -
    # so it is also the only place in the service layer where a state is
    # written down, and it is written from the modality, not from a branch on
    # some other state.
    en_linea = datos.modalidad_pago == ModalidadPago.EN_LINEA
    estado_inicial = (
        EstadoReserva.PENDIENTE_PAGO.value if en_linea else EstadoReserva.CONFIRMADA.value
    )
    creada_en = ahora_utc()

    reserva = reserva_repo.crear(
        db,
        codigo=_generar_codigo(db),
        codigo_qr=_generar_qr(db),
        usuario_id=usuario.id,
        vehiculo_id=vehiculo.id,
        servicio_id=servicio.id,
        bahia_id=libre.id,
        inicio=inicio_utc,
        fin=fin_utc,
        estado=estado_inicial,
        # RF-014 CA-03 + RF-012: the tariff is frozen here, and so is the
        # BREAKDOWN that explains it. A later price, factor or promotion change
        # (EXTENSION POINT P6) never moves either of them.
        monto_centimos=desglose.total_centimos,
        moneda=desglose.moneda,
        modalidad_pago=datos.modalidad_pago.value,
        # RF-014 flow 2a: "la reserva se mantiene en estado Pendiente de pago
        # durante 15 minutos". The scheduler of INC-5 sweeps this column.
        expira_en=(creada_en + pago_service.ventana_de_pago()) if en_linea else None,
        creada_en=creada_en,
    )

    tarifa_service.congelar(db, reserva, desglose)
    # RF-032: a coupon that came out of the loyalty programme is spent HERE,
    # after the breakdown froze it - so a coupon the tariff engine REJECTED
    # (flow 3a) is still there for the next attempt, and one that discounted
    # the booking cannot discount a second one.
    fidelizacion_service.consumir(db, reserva, desglose.cupon_aplicado)

    reserva_repo.agregar_historial(
        db,
        reserva_id=reserva.id,
        estado=reserva.estado,
        autor_id=usuario.id,
        ocurrido_en=ahora_utc(),
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        eventos.RESERVA_CREADA,
        autor_id=usuario.id,
        datos={
            "codigo": reserva.codigo,
            "servicio_id": servicio.id,
            "bahia_id": libre.id,
            "inicio": inicio_utc.isoformat(),
            "monto_centimos": reserva.monto_centimos,
        },
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        eventos.TARIFA_CALCULADA,
        autor_id=usuario.id,
        datos=desglose.a_datos(),
    )
    # RF-012 flows 3a and 4a: a rejected coupon and a total clamped to zero are
    # both REPORTED, never silent, and neither aborts the booking.
    tarifa_service.registrar_incidencias(
        db,
        desglose,
        entidad=eventos.ENTIDAD_RESERVA,
        entidad_id=reserva.id,
        autor_id=usuario.id,
    )

    # RF-022: the delivery time starts as the one the booking promised, and
    # every state change recalculates it from there.
    reserva.hora_estimada_entrega = fin_utc

    # RF-029: "confirmación" is one of the six lifecycle events, and it is the
    # one that is NOT a transition - creating a reservation has no origin state
    # to declare - so it is dispatched here instead of from the table.
    #
    # Only for a booking that was born CONFIRMED. An online one is not
    # confirmed yet, and the move that will confirm it
    # (``pendiente_pago -> confirmada``) already carries
    # ``evento_notificacion = 'confirmacion'``, so telling the customer here
    # too would promise them a slot the gateway has not paid for and then
    # promise it again.
    if not en_linea:
        notificacion_service.despachar(
            db,
            usuario,
            EventoNotificacion.CONFIRMACION.value,
            reserva=reserva,
            momento=ahora_utc(),
            **proveedores,
        )

    db.commit()
    db.refresh(reserva)

    if notificador is not NOTIFICADOR_PREDETERMINADO and not en_linea:
        notificador.notificar(
            usuario.id,
            "Reserva confirmada",
            f"Tu reserva {reserva.codigo} quedó confirmada para el {inicio_lima.isoformat()}.",
            {"reserva_id": reserva.id, "codigo": reserva.codigo},
        )
    return reserva


def _primera_bahia_libre(
    db: Session,
    inicio_utc: datetime,
    fin_utc: datetime,
    *,
    excluir_reserva_id: int | None = None,
):
    """The lowest-id active bay free in ``[inicio, fin)``, or ``None``.

    Free means: no active reservation overlapping (RN-03) and no blocking
    covering the slot (RF-018 CA-01).

    ``excluir_reserva_id`` is RF-015: a booking that is being MOVED must not
    count as an obstacle to itself, or shifting a 45 minute service by fifteen
    minutes would report its own bay as taken.
    """
    bahias = bahia_repo.listar_activas_bloqueadas(db)
    ocupadas = reserva_repo.bahias_ocupadas(
        db,
        inicio_utc,
        fin_utc,
        transicion_repo.listar_estados_no_terminales(db),
        excluir_reserva_id=excluir_reserva_id,
    )
    franjas = agenda_service.franjas_bloqueadas(
        db, a_lima(inicio_utc).date(), a_lima(fin_utc).date()
    )
    ocupadas = ocupadas | agenda_service.bahias_bloqueadas(
        franjas, [bahia.id for bahia in bahias], inicio_utc, fin_utc
    )
    return next((bahia for bahia in bahias if bahia.id not in ocupadas), None)


def atencion_inmediata(
    db: Session,
    datos: AtencionInmediataIn,
    autor: Usuario,
    permisos: list[str],
    *,
    notificador: Notificador = NOTIFICADOR_PREDETERMINADO,
    **proveedores,
) -> Reserva:
    """Serve a customer who arrived without booking (RF-019 flow 1a).

    The counter opens the service on the spot, "if a bay is free". RN-02 does
    not apply - the vehicle is already here - but RN-07 does: the shop cannot
    take work outside its opening hours or on a day it declared closed.

    The reservation is BORN confirmed and is immediately checked in through
    :func:`app.services.operacion_service.check_in`, so where a walk-in lands
    is still the row in ``transicion_estado`` and not a literal here (P3), and
    the timeline reads ``confirmada -> en_recepcion`` exactly like a booked one.
    """
    servicio = servicio_service.obtener_publico(db, datos.servicio_id)
    precio = servicio_service.precio_vigente(servicio)

    vehiculo = vehiculo_repo.obtener_por_id(db, datos.vehiculo_id)
    if vehiculo is None:
        raise RecursoNoEncontrado(
            "No encontramos ese vehículo. Regístralo antes de abrir la atención.",
            detalles=[detalle("vehiculo_id", "El vehículo no existe.")],
        )

    inicio_lima = a_lima(ahora())
    fin_lima = inicio_lima + timedelta(minutes=servicio.duracion_min)

    _exigir_horario(
        db,
        servicio,
        inicio_lima,
        fin_lima,
        alternativa=(
            f"El servicio dura {servicio.duracion_min} minutos y no alcanza antes del cierre."
        ),
    )

    inicio_utc = a_utc(inicio_lima)
    fin_utc = a_utc(fin_lima)
    desglose = tarifa_service.calcular(
        db,
        servicio_id=servicio.id,
        precio_base_centimos=precio.monto_centimos,
        moneda=precio.moneda,
        tipo_vehiculo=vehiculo.tipo,
        adicionales_ids=datos.adicionales,
        cupon=datos.cupon,
        fecha=inicio_lima.date(),
        usuario_id=vehiculo.usuario_id,
    )
    libre = _primera_bahia_libre(db, inicio_utc, fin_utc)
    if libre is None:
        raise ReservaBloqueOcupado(
            "No hay una bahía libre para atender ahora mismo. "
            "Ofrece al cliente una reserva para el siguiente bloque disponible.",
            detalles=[
                detalle("inicio", f"Bloque libre cercano: {bloque.inicio.isoformat()}.")
                for bloque in bloques_cercanos(db, servicio, inicio_lima)
            ],
        )

    reserva = reserva_repo.crear(
        db,
        codigo=_generar_codigo(db),
        codigo_qr=_generar_qr(db),
        usuario_id=vehiculo.usuario_id,
        vehiculo_id=vehiculo.id,
        servicio_id=servicio.id,
        bahia_id=libre.id,
        inicio=inicio_utc,
        fin=fin_utc,
        estado=EstadoReserva.CONFIRMADA.value,
        monto_centimos=desglose.total_centimos,
        moneda=desglose.moneda,
        # A walk-in pays at the counter by definition: the vehicle is already
        # here and RF-019 flow 1a has no gateway step.
        modalidad_pago=ModalidadPago.PRESENCIAL.value,
        atencion_sin_reserva=True,
    )
    tarifa_service.congelar(db, reserva, desglose)
    fidelizacion_service.consumir(db, reserva, desglose.cupon_aplicado)
    reserva.hora_estimada_entrega = fin_utc
    reserva_repo.agregar_historial(
        db,
        reserva_id=reserva.id,
        estado=reserva.estado,
        autor_id=autor.id,
        ocurrido_en=ahora_utc(),
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        eventos.RESERVA_ATENCION_INMEDIATA,
        autor_id=autor.id,
        datos={
            "codigo": reserva.codigo,
            "servicio_id": servicio.id,
            "bahia_id": libre.id,
            "inicio": inicio_utc.isoformat(),
            "monto_centimos": reserva.monto_centimos,
        },
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        eventos.TARIFA_CALCULADA,
        autor_id=autor.id,
        datos=desglose.a_datos(),
    )
    tarifa_service.registrar_incidencias(
        db,
        desglose,
        entidad=eventos.ENTIDAD_RESERVA,
        entidad_id=reserva.id,
        autor_id=autor.id,
    )

    # The check-in commits the whole thing, so a rejected entry leaves no
    # half-created reservation behind.
    return operacion_service.check_in(
        db,
        reserva,
        CheckInIn(observaciones=datos.observaciones, confirmar_retraso=True),
        autor,
        permisos,
        notificador=notificador,
        **proveedores,
    )


def normalizar_estados(estados: list[str] | None) -> list[str]:
    """Clean the ``estado`` query parameter into a usable set (RF-017 v1.0).

    Empty strings are dropped, duplicates collapse and the list is capped. The
    values are NOT validated against a catalogue of states on purpose: the
    machine lives in ``transicion_estado`` (P3), so a state inserted as data is
    filterable the same afternoon, and a state that does not exist simply
    matches nothing - which is the honest answer to asking for it.
    """
    limpios = [valor.strip()[:30] for valor in (estados or []) if valor and valor.strip()]
    return list(dict.fromkeys(limpios))[:MAXIMO_ESTADOS_FILTRO]


def _limites_de_fechas(desde, hasta) -> tuple[datetime | None, datetime | None]:
    """Turn the Lima dates of the filter into the UTC bounds of ``inicio``.

    A day is a LOCAL thing and the column is not, which is why this conversion
    exists at all: ``hasta`` is inclusive for the customer, so the upper bound
    is the start of the following day and the comparison stays half open.
    """
    inicio = a_utc(datetime.combine(desde, time.min, tzinfo=ZONA_LIMA)) if desde else None
    fin = (
        a_utc(datetime.combine(hasta + timedelta(days=1), time.min, tzinfo=ZONA_LIMA))
        if hasta
        else None
    )
    return inicio, fin


def listar(
    db: Session,
    actual: Usuario,
    permisos: list[str],
    *,
    estados: list[str] | None = None,
    vehiculo_id: int | None = None,
    desde: date | None = None,
    hasta: date | None = None,
    pagina: int = 1,
    tamanio: int = TAMANIO_PAGINA_DEFECTO,
) -> tuple[list[Reserva], int, int, int]:
    """One page of reservations, newest first (RF-017 + its v1.0 delta).

    Horizontal authorization: without ``reserva:leer_todas`` the caller only
    ever sees rows whose ``usuario_id`` is their own (CA-03).

    The v1.0 delta is the three filters that join the state one: **several
    states at once**, **one vehicle** (CA-02) and **a date range**. The
    multi-state form is what closes the gap the mobile client had been paying
    for: ``(cliente)/inicio`` and ``(personal)/operacion`` are aggregate
    screens that ask for "everything still in progress", and with a single
    ``estado`` the only way to answer that was one request per active state.
    """
    pagina = max(1, int(pagina or 1))
    tamanio = max(1, min(int(tamanio or TAMANIO_PAGINA_DEFECTO), TAMANIO_PAGINA_MAXIMO))

    if desde is not None and hasta is not None and hasta < desde:
        raise DatosInvalidos(
            "El rango de fechas está invertido: la fecha final es anterior a la inicial. "
            "Corrige el rango e inténtalo de nuevo.",
            detalles=[detalle("hasta", "Debe ser igual o posterior a «desde».")],
        )

    limite_inferior, limite_superior = _limites_de_fechas(desde, hasta)
    filtro_usuario = None if puede_ver_todas(permisos) else actual.id
    items, total = reserva_repo.listar_paginado(
        db,
        usuario_id=filtro_usuario,
        estados=normalizar_estados(estados),
        vehiculo_id=vehiculo_id,
        desde=limite_inferior,
        hasta=limite_superior,
        pagina=pagina,
        tamanio=tamanio,
    )
    return items, total, pagina, tamanio


def obtener(db: Session, reserva_id: int, actual: Usuario, permisos: list[str]) -> Reserva:
    """One reservation the caller is allowed to read (RF-017 CA-03, RF-022)."""
    reserva = reserva_repo.obtener_por_id(db, reserva_id)
    if reserva is None:
        raise RecursoNoEncontrado("No encontramos esa reserva.")
    if not puede_ver_todas(permisos) and reserva.usuario_id != actual.id:
        # Same 404 as a missing row: a client must not be able to probe ids.
        raise RecursoNoEncontrado("No encontramos esa reserva.")
    return reserva


def puede_reprogramarse(db: Session, reserva: Reserva) -> bool:
    """Whether the booking is still in a state that admits a move (RF-015).

    RF-015's precondition reads "existe una reserva en estado Confirmada y no
    ha iniciado", and this is that sentence WITHOUT naming a state (P3): a
    reservation may be moved while the check-in operation still declares a move
    out of where it is. One that already drove into the shop has no check-in
    left, a cancelled one has no outgoing move at all, and one still waiting
    for its online payment does not declare the check-in either - it declares
    the payment. All three fall out of one question asked of the table, and a
    twelfth state inserted as data answers it by itself.
    """
    return operacion_service.tiene_operacion_declarada(
        db, reserva, operacion_service.ENDPOINT_CHECK_IN
    )


def _exigir_reprogramable(db: Session, reserva: Reserva, momento: datetime) -> None:
    """The three gates of RF-015, in the order that gives the best reason.

    RN-06 is checked BEFORE the two-hour window on purpose: a booking that has
    spent its allowance gets the same answer whenever it asks, and the answer
    that helps ("cancel and book again") does not depend on what time it is.
    """
    if not puede_reprogramarse(db, reserva):
        raise TransicionInvalida(
            "Esa reserva ya no admite una reprogramación. "
            "Consulta su estado actual antes de intentarlo de nuevo.",
            detalles=[
                detalle(
                    "estado",
                    f"La reserva está en «{reserva.estado}» y esta operación no aplica "
                    "a ese estado.",
                )
            ],
        )

    # RN-06, the whole rule, in the only place it exists.
    if reserva.reprogramaciones_count >= MAXIMO_REPROGRAMACIONES:
        raise LimiteDeReprogramaciones(
            detalles=[
                detalle("reprogramaciones", str(reserva.reprogramaciones_count)),
                detalle(
                    "maximo",
                    f"RN-06 permite {MAXIMO_REPROGRAMACIONES} reprogramaciones por reserva.",
                ),
            ]
        )

    # RF-015 flow 2b. Strict, like RN-05: exactly two hours is already late.
    restante = desde_bd(reserva.inicio) - momento
    if restante <= ANTICIPACION_REPROGRAMACION:
        raise ReprogramacionFueraDePlazo(
            detalles=[
                detalle(
                    "inicio",
                    "Faltan menos de dos horas para el inicio de la reserva.",
                ),
                detalle("minutos_restantes", str(max(0, int(restante.total_seconds() // 60)))),
            ]
        )


def reprogramar(
    db: Session,
    reserva: Reserva,
    datos: ReprogramacionIn,
    autor: Usuario,
    permisos: list[str],
    *,
    notificador: Notificador = NOTIFICADOR_PREDETERMINADO,
    confirmar: bool = True,
    **proveedores,
) -> Reserva:
    """Move a confirmed booking to another block (RF-015, RN-06, RN-03).

    The permission is checked HERE and not only at the router, for the same
    reason the transition checks it inside ``cambiar_estado``: this operation
    has two doors, and the second one - the answer to the reminder of RF-030 -
    is guarded by the reading permissions, which the bay operator holds.

    The reservation is EDITED IN PLACE - ``inicio``, ``fin`` and ``bahia_id``
    move, the row does not. That is what keeps the code the customer already
    knows, the payment, the receipt, the evidence and the frozen breakdown
    attached to the service they belong to; creating a replacement row and
    cancelling the original would scatter all of them and would make RN-05
    charge a cancellation penalty for a service nobody cancelled.

    The old block is released by the very same edit: the row stops overlapping
    it, so the next availability query offers it again. That is CA-02, and it
    needs no bookkeeping of its own.

    The tariff is NOT recomputed. RF-014 CA-03 froze it when the booking was
    created and RF-015 changes WHEN the service happens, not WHAT it costs;
    repricing here would mean a customer could be quietly charged more for
    accepting the shop's own suggestion to move.

    ``notificación del cambio`` (RF-015 "Salidas") is a template and an event,
    like every other notice since INC-5 - see ``EventoNotificacion.REPROGRAMACION``.
    """
    if PERMISO_REPROGRAMAR not in set(permisos or ()):
        raise PermisoDenegado(
            detalles=[detalle("reprogramacion", f"Se requiere el permiso {PERMISO_REPROGRAMAR}.")]
        )

    momento = ahora_utc()
    _exigir_reprogramable(db, reserva, momento)

    servicio = reserva.servicio
    inicio_lima = a_lima(datos.inicio)
    fin_lima = inicio_lima + timedelta(minutes=servicio.duracion_min)

    _exigir_anticipacion(inicio_lima)
    _exigir_horario(
        db,
        servicio,
        inicio_lima,
        fin_lima,
        alternativa=(
            f"El servicio dura {servicio.duracion_min} minutos y debe terminar antes del cierre."
        ),
    )

    inicio_utc = a_utc(inicio_lima)
    fin_utc = a_utc(fin_lima)

    # RN-03: one bay, one vehicle at a time. The booking being moved is
    # excluded from its own occupancy, so a small shift is not blocked by the
    # block it is leaving.
    libre = _primera_bahia_libre(db, inicio_utc, fin_utc, excluir_reserva_id=reserva.id)
    if libre is None:
        raise ReservaBloqueOcupado(
            detalles=[
                detalle("inicio", f"Bloque libre cercano: {bloque.inicio.isoformat()}.")
                for bloque in bloques_cercanos(db, servicio, inicio_lima)
            ]
            or [detalle("inicio", "No quedan bloques libres ese día. Prueba con otra fecha.")]
        )

    anterior = {
        "inicio": desde_bd(reserva.inicio).isoformat(),
        "fin": desde_bd(reserva.fin).isoformat(),
        "bahia_id": reserva.bahia_id,
    }

    reserva.inicio = inicio_utc
    reserva.fin = fin_utc
    reserva.bahia_id = libre.id
    reserva.reprogramaciones_count += 1
    # RF-022: the promise the booking makes moves with it.
    reserva.hora_estimada_entrega = fin_utc

    _rearmar_recordatorio(db, reserva, inicio_utc)

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_RESERVA,
        reserva.id,
        eventos.RESERVA_REPROGRAMADA,
        autor_id=autor.id,
        datos={
            "codigo": reserva.codigo,
            "anterior": anterior,
            "inicio": inicio_utc.isoformat(),
            "fin": fin_utc.isoformat(),
            "bahia_id": libre.id,
            "reprogramaciones": reserva.reprogramaciones_count,
            "restantes": MAXIMO_REPROGRAMACIONES - reserva.reprogramaciones_count,
        },
    )

    notificacion_service.despachar(
        db,
        reserva.usuario,
        EventoNotificacion.REPROGRAMACION.value,
        reserva=reserva,
        datos={"reprogramaciones": reserva.reprogramaciones_count},
        momento=momento,
        **proveedores,
    )

    if confirmar:
        db.commit()
        db.refresh(reserva)

    # P8: an injected notifier is still told, on top of the persisted channels.
    if notificador is not NOTIFICADOR_PREDETERMINADO:
        notificador.notificar(
            reserva.usuario_id,
            "Reserva reprogramada",
            f"Tu reserva {reserva.codigo} quedó reprogramada para el {inicio_lima.isoformat()}.",
            {"reserva_id": reserva.id, "inicio": inicio_lima.isoformat()},
        )
    return reserva


def _rearmar_recordatorio(db: Session, reserva: Reserva, inicio_utc: datetime) -> None:
    """Point the two-hour reminder at the new block (RF-030 after RF-015).

    The reminder that was armed for the old block no longer means anything. It
    is RE-ARMED rather than deleted because the row is unique per reservation
    and carries the answer that may well have caused this move; clearing
    ``enviado_en`` is all the sweep looks at, so the customer is reminded again
    before the block they actually have now.
    """
    fila = notificacion_repo.obtener_recordatorio(db, reserva.id)
    if fila is None:
        return
    notificacion_repo.rearmar_recordatorio(
        db,
        fila,
        programado_para=inicio_utc - VENTANA_RECORDATORIO,
        estado=EstadoRecordatorio.PENDIENTE.value,
    )


def cancelar(
    db: Session,
    reserva: Reserva,
    motivo: str,
    autor: Usuario,
    permisos: list[str],
    *,
    politica: PoliticaCancelacion = POLITICA_PREDETERMINADA,
    notificador: Notificador = NOTIFICADOR_PREDETERMINADO,
    **proveedores,
) -> tuple[Reserva, Dinero]:
    """Cancel a reservation and free its block (RF-016 + RN-05).

    Which states may still be cancelled from - and where the cancellation lands
    - is read from ``transicion_estado`` (P3), so a reservation whose state no
    longer declares the move is refused with 422 (CA-02) and v1.0 opening the
    cancellation after the check-in (``en_recepcion``) was a row, not a branch.

    The penalty comes from the injected policy, which since INC-4 is RN-05 for
    real: zero more than two hours ahead (CA-01), 20 % of the amount under that
    (CA-02). The FLOW did not change to get there - that is what the policy
    object was for.

    Step 5 of the v1.0 delta - "el sistema inicia el reembolso correspondiente
    si el pago fue en línea" - is :func:`_reembolsar_cancelacion` below, and
    flow 5a is already in it: a reversal the gateway refuses becomes a
    ``pendiente_manual`` row instead of an exception nobody reads.
    """
    momento = ahora_utc()
    penalidad = politica.calcular_penalidad(reserva, momento)
    motivo_limpio = motivo.strip()
    destino = operacion_service.destino_declarado(
        db, reserva, operacion_service.ENDPOINT_CANCELACION
    )

    # The transition is validated (and may be refused with 422) before any
    # cancellation field is touched, so a rejected attempt leaves no trace.
    operacion_service.cambiar_estado(
        db,
        reserva,
        destino,
        autor,
        permisos,
        origen_llamada=operacion_service.ENDPOINT_CANCELACION,
        accion=eventos.RESERVA_CANCELADA,
        datos={"motivo": motivo_limpio, "penalidad_centimos": penalidad.monto_centimos},
        notificador=notificador,
        confirmar=False,
        **proveedores,
    )

    # RF-016 CA-03: the reason, the author and the moment stay on the record.
    reserva.motivo_cancelacion = motivo_limpio
    reserva.cancelada_por_id = autor.id
    reserva.cancelada_en = momento
    # RN-05: what was kept is part of the record too, so the "resumen de la
    # cancelación" of step 3 can be read again later.
    reserva.penalidad_centimos = penalidad.monto_centimos
    # Nothing is waiting for the money any more.
    reserva.expira_en = None

    _reembolsar_cancelacion(db, reserva, penalidad, autor, momento=momento, **proveedores)

    db.commit()
    db.refresh(reserva)
    return reserva, penalidad


def _reembolsar_cancelacion(
    db: Session,
    reserva: Reserva,
    penalidad: Dinero,
    autor: Usuario,
    *,
    momento: datetime,
    **proveedores,
) -> None:
    """Give back what RN-05 does not keep, when there is anything to give back.

    "Si el pago fue en línea" is read from the PAYMENT and not from
    ``reserva.modalidad_pago``: what decides whether a reversal can be
    automated is whether a gateway took the money, and a booking may well have
    changed modality along the way (RF-025 flow 4a). A counter payment is
    returned over the counter, through ``POST /pagos/{id}/reembolsos``.
    """
    pago = pago_repo.obtener_confirmado(db, reserva.id)
    if pago is None or not pago.pasarela:
        return

    devolver = pago.saldo_centimos - penalidad.monto_centimos
    if devolver <= 0:
        # The penalty ate the whole balance. Nothing to reverse, and saying so
        # in the log beats a refund row for zero soles.
        return

    tipo = (
        TipoReembolso.TOTAL.value
        if devolver >= pago.saldo_centimos
        else TipoReembolso.PARCIAL.value
    )
    reembolso_service.solicitar(
        db,
        pago,
        tipo=tipo,
        motivo=MOTIVO_REEMBOLSO_CANCELACION.format(codigo=reserva.codigo),
        idempotency_key=f"cancelacion-{reserva.id}-{pago.id}",
        monto_centimos=devolver,
        autor=autor,
        momento=momento,
        # The cancellation owns the transaction: one commit, one outcome.
        confirmar=False,
        **proveedores,
    )


def fin_estimado(reserva: Reserva) -> datetime:
    """Best known finishing time: the real one, the one implied by the entry
    time, or the originally scheduled end (RF-022)."""
    if reserva.hora_fin_real is not None:
        return desde_bd(reserva.hora_fin_real)
    if reserva.hora_ingreso is not None:
        duracion = timedelta(minutes=reserva.servicio.duracion_min)
        return desde_bd(reserva.hora_ingreso) + duracion
    return desde_bd(reserva.fin)
