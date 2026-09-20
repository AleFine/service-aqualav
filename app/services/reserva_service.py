"""Reservation lifecycle: creation, listing, detail and cancellation.

RF-014, RF-016 and RF-017. The concurrency-sensitive part is :func:`crear`:
the bay set is locked for the duration of the transaction, so two simultaneous
confirmations over the same block produce exactly one 201 and one 409 (CA-02).
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.codigos import generar_codigo_reserva
from app.core.errors import (
    RecursoNoEncontrado,
    ReservaAnticipacionInsuficiente,
    ReservaBloqueOcupado,
    ReservaFueraDeHorario,
    detalle,
)
from app.core.horario import a_lima, a_utc, ahora, ahora_utc, dentro_de_horario, desde_bd
from app.models import EstadoReserva, ModalidadPago, Reserva, Usuario
from app.repositories import bahia as bahia_repo
from app.repositories import reserva as reserva_repo
from app.repositories import transicion as transicion_repo
from app.repositories import vehiculo as vehiculo_repo
from app.schemas import TAMANIO_PAGINA_DEFECTO, TAMANIO_PAGINA_MAXIMO, Dinero, ReservaCrear
from app.services import eventos, operacion_service, servicio_service
from app.services.disponibilidad_service import ANTICIPACION_MINIMA, bloques_cercanos
from app.services.notificador import NOTIFICADOR_PREDETERMINADO, Notificador
from app.services.politica_cancelacion import POLITICA_PREDETERMINADA, PoliticaCancelacion

#: Permission that lifts the "only your own reservations" filter (RF-017 CA-03).
PERMISO_LEER_TODAS = "reserva:leer_todas"

#: How many times to retry on a reservation code collision before giving up.
INTENTOS_CODIGO = 5


def _generar_codigo(db: Session) -> str:
    """A unique ``AQL-XXXXXX`` code. Collisions are astronomically unlikely."""
    for _ in range(INTENTOS_CODIGO):
        codigo = generar_codigo_reserva()
        if not reserva_repo.existe_codigo(db, codigo):
            return codigo
    return generar_codigo_reserva()  # pragma: no cover - practically unreachable


def puede_ver_todas(permisos: list[str]) -> bool:
    return PERMISO_LEER_TODAS in set(permisos or ())


def crear(
    db: Session,
    usuario: Usuario,
    datos: ReservaCrear,
    *,
    notificador: Notificador = NOTIFICADOR_PREDETERMINADO,
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

    inicio_lima = a_lima(datos.inicio)
    fin_lima = inicio_lima + timedelta(minutes=servicio.duracion_min)

    if inicio_lima < a_lima(ahora()) + ANTICIPACION_MINIMA:
        raise ReservaAnticipacionInsuficiente(
            detalles=[detalle("inicio", "Elige un bloque con al menos 60 minutos de anticipación.")]
        )

    if not dentro_de_horario(inicio_lima, fin_lima):
        raise ReservaFueraDeHorario(
            detalles=[
                detalle(
                    "inicio",
                    f"El servicio dura {servicio.duracion_min} minutos y debe terminar "
                    "antes del cierre.",
                )
            ]
        )

    inicio_utc = a_utc(inicio_lima)
    fin_utc = a_utc(fin_lima)

    # Serialization point: every concurrent creation queues on this lock, so
    # the overlap check below cannot be raced (RF-014 CA-02).
    bahias = bahia_repo.listar_activas_bloqueadas(db)
    ocupadas = reserva_repo.bahias_ocupadas(
        db, inicio_utc, fin_utc, transicion_repo.listar_estados_no_terminales(db)
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

    reserva = reserva_repo.crear(
        db,
        codigo=_generar_codigo(db),
        usuario_id=usuario.id,
        vehiculo_id=vehiculo.id,
        servicio_id=servicio.id,
        bahia_id=libre.id,
        inicio=inicio_utc,
        fin=fin_utc,
        # CREATION, the one edge of Annex A that is not a row in
        # ``transicion_estado``: there is no origin state to look the move up
        # by. A presential booking is born confirmed (transition 2); INC-4 adds
        # the online branch, which is born ``pendiente_pago`` (transition 1).
        estado=EstadoReserva.CONFIRMADA.value,
        # RF-014 CA-03: the tariff is frozen here; a later price change
        # (EXTENSION POINT P6) never moves it.
        monto_centimos=precio.monto_centimos,
        moneda=precio.moneda,
        modalidad_pago=ModalidadPago.PRESENCIAL.value,
    )

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

    db.commit()
    db.refresh(reserva)

    notificador.notificar(
        usuario.id,
        "Reserva confirmada",
        f"Tu reserva {reserva.codigo} quedó confirmada para el {inicio_lima.isoformat()}.",
        {"reserva_id": reserva.id, "codigo": reserva.codigo},
    )
    return reserva


def listar(
    db: Session,
    actual: Usuario,
    permisos: list[str],
    *,
    estado: str | None = None,
    pagina: int = 1,
    tamanio: int = TAMANIO_PAGINA_DEFECTO,
) -> tuple[list[Reserva], int, int, int]:
    """One page of reservations, newest first (RF-017).

    Horizontal authorization: without ``reserva:leer_todas`` the caller only
    ever sees rows whose ``usuario_id`` is their own (CA-03).
    """
    pagina = max(1, int(pagina or 1))
    tamanio = max(1, min(int(tamanio or TAMANIO_PAGINA_DEFECTO), TAMANIO_PAGINA_MAXIMO))

    filtro_usuario = None if puede_ver_todas(permisos) else actual.id
    items, total = reserva_repo.listar_paginado(
        db, usuario_id=filtro_usuario, estado=estado, pagina=pagina, tamanio=tamanio
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


def cancelar(
    db: Session,
    reserva: Reserva,
    motivo: str,
    autor: Usuario,
    permisos: list[str],
    *,
    politica: PoliticaCancelacion = POLITICA_PREDETERMINADA,
    notificador: Notificador = NOTIFICADOR_PREDETERMINADO,
) -> tuple[Reserva, Dinero]:
    """Cancel a reservation and free its block (RF-016).

    Which states may still be cancelled from - and where the cancellation lands
    - is read from ``transicion_estado`` (P3), so a reservation whose state no
    longer declares the move is refused with 422 (CA-02) and v1.0 opening the
    cancellation after the check-in (``en_recepcion``) was a row, not a branch.
    The penalty comes from the injected policy - always zero in the MVP.
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
    )

    # RF-016 CA-03: the reason, the author and the moment stay on the record.
    reserva.motivo_cancelacion = motivo_limpio
    reserva.cancelada_por_id = autor.id
    reserva.cancelada_en = momento

    db.commit()
    db.refresh(reserva)
    return reserva, penalidad


def fin_estimado(reserva: Reserva) -> datetime:
    """Best known finishing time: the real one, the one implied by the entry
    time, or the originally scheduled end (RF-022)."""
    if reserva.hora_fin_real is not None:
        return desde_bd(reserva.hora_fin_real)
    if reserva.hora_ingreso is not None:
        duracion = timedelta(minutes=reserva.servicio.duracion_min)
        return desde_bd(reserva.hora_ingreso) + duracion
    return desde_bd(reserva.fin)
