"""Bay and operator assignment, and the waiting queue (RF-020).

The counter receives the vehicle (RF-019) and this module decides where it goes
and who works on it. It SUGGESTS the free bay and the least loaded operator,
accepts an override, and answers honestly when the shop has no room: the
service stays in the queue with an estimate instead of pretending it was
assigned (flow 2a).

Neither end of the ``en_recepcion -> asignado`` move is written here: the row
in ``transicion_estado`` names this operation as its owner and says where it
leads (P3), and who may perform it is a permission code, never a role name (P5).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.errors import (
    DatosInvalidos,
    OperarioOcupado,
    RecursoNoEncontrado,
    ReservaBloqueOcupado,
    SinOperarioDisponible,
    detalle,
)
from app.core.horario import a_lima, ahora_utc, desde_bd
from app.models import (
    AsignacionServicio,
    Bahia,
    ColaEspera,
    EstadoCuenta,
    EventoNotificacion,
    Reserva,
    Usuario,
)
from app.repositories import asignacion as asignacion_repo
from app.repositories import bahia as bahia_repo
from app.repositories import reserva as reserva_repo
from app.repositories import transicion as transicion_repo
from app.repositories import usuario as usuario_repo
from app.schemas import AsignacionIn
from app.services import (
    agenda_service,
    bahia_service,
    eventos,
    notificacion_service,
    operacion_service,
    reserva_service,
)
from app.services.notificador import NOTIFICADOR_PREDETERMINADO, Notificador

#: Permission that lets somebody assign a service (principle P5).
PERMISO_ASIGNAR = "reserva:asignar"

#: The permission that MAKES somebody an operator. RF-020 needs "the operators"
#: and resolves them by what they are allowed to do, never by a role name, so a
#: shop that invents a new bay role gets its people suggested for free.
PERMISO_OPERAR = "reserva:avanzar_estado"


@dataclass(frozen=True)
class ResultadoAsignacion:
    """Outcome of one assignment attempt. Exactly one of the two is filled."""

    reserva: Reserva
    asignacion: AsignacionServicio | None = None
    cola: ColaEspera | None = None
    bahia_sugerida: Bahia | None = None
    operario_sugerido: Usuario | None = None


def _ventana(reserva: Reserva) -> tuple[datetime, datetime]:
    return desde_bd(reserva.inicio), desde_bd(reserva.fin)


def _operarios(db: Session) -> list[Usuario]:
    """Active accounts able to execute a service, lowest id first."""
    return [
        usuario
        for usuario in usuario_repo.listar_con_permiso(db, PERMISO_OPERAR)
        if usuario.estado_cuenta == EstadoCuenta.ACTIVA.value
    ]


def _bahias_libres(db: Session, reserva: Reserva, estados_activos: set[str]) -> list[Bahia]:
    """Bays that can take this service right now (RF-020 step 2)."""
    inicio, fin = _ventana(reserva)
    franjas = agenda_service.franjas_bloqueadas(db, a_lima(inicio).date(), a_lima(fin).date())
    ids = [bahia.id for bahia in bahia_repo.listar_activas(db)]
    bloqueadas = agenda_service.bahias_bloqueadas(franjas, ids, inicio, fin)
    return bahia_service.libres_para(
        db,
        inicio,
        fin,
        estados_activos,
        bloqueadas=bloqueadas,
        excluir_reserva_id=reserva.id,
    )


def _sugerir_bahia(reserva: Reserva, libres: list[Bahia]) -> Bahia | None:
    """The bay the reservation already booked if it is free, else the first one."""
    if not libres:
        return None
    propia = next((bahia for bahia in libres if bahia.id == reserva.bahia_id), None)
    return propia or libres[0]


def _sugerir_operario(
    operarios: list[Usuario], carga: dict[int, int], bahia: Bahia | None
) -> Usuario | None:
    """The operator RF-020 step 2 pre-selects, by four criteria in this order.

    1. the LEAST LOADED, which is what the requirement asks for literally;
    2. the one whose usual bay this is (``bahia_habitual``, RF-035);
    3. the one with the NARROWEST authority. An administrator holds every
       permission, ``reserva:avanzar_estado`` among them, so they are a valid
       target - covering a shift is legitimate - but they are not who the shop
       means by "the operator". Counting permission codes says that without
       naming a single role (principle P5), and it degrades gracefully: when
       the administrator is the only candidate left, they get suggested;
    4. the lowest id, so the suggestion is stable between calls.
    """
    if not operarios:
        return None
    return min(
        operarios,
        key=lambda usuario: (
            carga.get(usuario.id, 0),
            0 if bahia is not None and usuario.bahia_habitual_id == bahia.id else 1,
            len(usuario.rol.codigos_permisos),
            usuario.id,
        ),
    )


def sugerencia(db: Session, reserva: Reserva) -> tuple[Bahia | None, Usuario | None]:
    """What the system would pick, so the screen can pre-select it (step 2)."""
    estados_activos = transicion_repo.listar_estados_no_terminales(db)
    bahia = _sugerir_bahia(reserva, _bahias_libres(db, reserva, estados_activos))
    carga = asignacion_repo.carga_por_operario(db, estados_activos)
    return bahia, _sugerir_operario(_operarios(db), carga, bahia)


# --------------------------------------------------------------------------
# Waiting queue (RF-020 flow 2a)
# --------------------------------------------------------------------------
def _depurar_cola(db: Session) -> list[ColaEspera]:
    """Drop the rows of reservations that already left the counter.

    "Still waiting" means the assignment operation is still declared for the
    current state - read from ``transicion_estado``, so no state is named here.
    """
    vigentes: list[ColaEspera] = []
    for fila in asignacion_repo.listar_cola(db):
        reserva = fila.reserva
        if reserva is None or not operacion_service.tiene_operacion_declarada(
            db, reserva, operacion_service.ENDPOINT_ASIGNACION
        ):
            asignacion_repo.desencolar(db, fila)
            continue
        vigentes.append(fila)

    for posicion, fila in enumerate(vigentes, start=1):
        fila.posicion = posicion
    db.flush()
    return vigentes


def encolar(db: Session, reserva: Reserva, estados_activos: set[str]) -> ColaEspera:
    """Keep a received vehicle in the queue with an estimate (flow 2a)."""
    vigentes = _depurar_cola(db)
    ya_estaba = next((fila for fila in vigentes if fila.reserva_id == reserva.id), None)
    posicion = ya_estaba.posicion if ya_estaba is not None else len(vigentes) + 1
    espera = _estimar_espera(db, reserva, posicion, estados_activos)
    return asignacion_repo.encolar(
        db, reserva_id=reserva.id, posicion=posicion, tiempo_estimado_min=espera
    )


def _estimar_espera(db: Session, reserva: Reserva, posicion: int, estados_activos: set[str]) -> int:
    """Minutes until this vehicle can reasonably enter a bay (flow 2a).

    The first bay frees when the earliest service in progress is due to finish
    (``fin_estimado``, RF-022); everybody ahead in the queue adds their own
    service duration on top. It is an ESTIMATE the counter reads out loud, not
    a promise the system keeps.
    """
    momento = ahora_utc()
    pendientes = [
        max(0, int((reserva_service.fin_estimado(fila.reserva) - momento).total_seconds() // 60))
        for fila in asignacion_repo.listar_activas(db, estados_activos)
        if fila.reserva is not None
    ]
    primera_liberacion = min(pendientes) if pendientes else 0
    return primera_liberacion + (posicion - 1) * reserva.servicio.duracion_min


def cola_del_operario(db: Session, operario: Usuario) -> list[Reserva]:
    """The operator's own queue: what they have to work on (RF-020 CA-02)."""
    estados_activos = transicion_repo.listar_estados_no_terminales(db)
    ids = asignacion_repo.listar_reservas_de_operario(db, operario.id, estados_activos)
    return reserva_repo.listar_por_ids(db, ids)


# --------------------------------------------------------------------------
# The operation itself
# --------------------------------------------------------------------------
def asignar(
    db: Session,
    reserva: Reserva,
    datos: AsignacionIn,
    autor: Usuario,
    permisos: list[str],
    *,
    notificador: Notificador = NOTIFICADOR_PREDETERMINADO,
    **proveedores,
) -> ResultadoAsignacion:
    """Assign a bay and an operator, or queue the service (RF-020).

    Order of the checks: the move has to be declared and allowed BEFORE
    anything is written, so a reservation that was never checked in gets 422
    and not a queue row it does not deserve.
    """
    destino = operacion_service.destino_declarado(
        db, reserva, operacion_service.ENDPOINT_ASIGNACION
    )
    operacion_service.validar_transicion(
        db,
        reserva,
        destino,
        permisos,
        origen_llamada=operacion_service.ENDPOINT_ASIGNACION,
    )

    estados_activos = transicion_repo.listar_estados_no_terminales(db)
    libres = _bahias_libres(db, reserva, estados_activos)
    carga = asignacion_repo.carga_por_operario(db, estados_activos)
    operarios = _operarios(db)
    bahia_sugerida = _sugerir_bahia(reserva, libres)
    operario_sugerido = _sugerir_operario(operarios, carga, bahia_sugerida)

    bahia = bahia_sugerida
    if datos.bahia_id is not None:
        bahia = next((libre for libre in libres if libre.id == datos.bahia_id), None)
        if bahia is None:
            if bahia_repo.obtener_por_id(db, datos.bahia_id) is None:
                raise RecursoNoEncontrado(
                    "No encontramos esa bahía.",
                    detalles=[detalle("bahia_id", "La bahía no existe.")],
                )
            raise ReservaBloqueOcupado(
                "La bahía que elegiste no está libre para este servicio. "
                "Elige una de las bahías disponibles o deja que el sistema sugiera una.",
                detalles=[detalle("bahia_id", f"Bahías libres: {_nombres(libres) or 'ninguna'}.")],
            )

    if bahia is None:
        # Flow 2a: the shop received the vehicle but has nowhere to put it. The
        # reservation does NOT move; the same call assigns it once a bay frees.
        fila = encolar(db, reserva, estados_activos)
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_RESERVA,
            reserva.id,
            eventos.RESERVA_ENCOLADA,
            autor_id=autor.id,
            datos={"posicion": fila.posicion, "tiempo_estimado_min": fila.tiempo_estimado_min},
        )
        db.commit()
        db.refresh(reserva)
        return ResultadoAsignacion(
            reserva=reserva,
            cola=fila,
            bahia_sugerida=None,
            operario_sugerido=operario_sugerido,
        )

    if not operarios:
        raise SinOperarioDisponible(
            detalles=[detalle("operario_id", f"Ningún usuario tiene {PERMISO_OPERAR}.")]
        )

    operario = operario_sugerido
    if datos.operario_id is not None:
        operario = next((cand for cand in operarios if cand.id == datos.operario_id), None)
        if operario is None:
            raise DatosInvalidos(
                "La persona que elegiste no puede ejecutar servicios. "
                "Elige a un operario activo de la lista.",
                detalles=[detalle("operario_id", f"Se requiere el permiso {PERMISO_OPERAR}.")],
            )

    ocupado = carga.get(operario.id, 0)
    if ocupado > 0 and not datos.confirmar_operario_ocupado:
        # Flow 3a: same shape as the late arrival of RF-019 - the counter is
        # told, and decides.
        raise OperarioOcupado(
            detalles=[
                detalle(
                    "confirmar_operario_ocupado",
                    f"{operario.nombre_completo} ya tiene " f"{ocupado} servicio(s) en curso.",
                ),
                detalle("operario_id", str(operario.id)),
            ]
        )

    sugerida = datos.bahia_id is None and datos.operario_id is None
    momento = ahora_utc()

    reserva.bahia_id = bahia.id
    asignacion = asignacion_repo.guardar(
        db,
        reserva_id=reserva.id,
        bahia_id=bahia.id,
        operario_id=operario.id,
        asignado_por_id=autor.id,
        asignado_en=momento,
        sugerida=sugerida,
    )
    bahia_service.ocupar(db, bahia)

    en_cola = asignacion_repo.obtener_cola_por_reserva(db, reserva.id)
    if en_cola is not None:
        asignacion_repo.desencolar(db, en_cola)

    operacion_service.cambiar_estado(
        db,
        reserva,
        destino,
        autor,
        permisos,
        origen_llamada=operacion_service.ENDPOINT_ASIGNACION,
        accion=eventos.RESERVA_ASIGNADA,
        datos={
            "bahia_id": bahia.id,
            "operario_id": operario.id,
            "sugerida": sugerida,
        },
        notificador=notificador,
        **proveedores,
    )
    _depurar_cola(db)
    db.commit()
    db.refresh(reserva)
    db.refresh(asignacion)

    # RF-020 step 5: the operator finds the service in their queue. It is a
    # notice like any other now, so it lands in ``notificacion`` with its own
    # template and its own delivery record instead of only in the log.
    notificacion_service.despachar(
        db,
        operario,
        EventoNotificacion.ASIGNACION.value,
        reserva=reserva,
        momento=momento,
        **proveedores,
    )
    db.commit()
    if notificador is not NOTIFICADOR_PREDETERMINADO:
        notificador.notificar(
            operario.id,
            "Tienes un servicio asignado",
            f"La reserva {reserva.codigo} te fue asignada en la {bahia.nombre}.",
            {"reserva_id": reserva.id, "bahia_id": bahia.id},
        )
    return ResultadoAsignacion(
        reserva=reserva,
        asignacion=asignacion,
        bahia_sugerida=bahia_sugerida,
        operario_sugerido=operario_sugerido,
    )


def _nombres(bahias: list[Bahia]) -> str:
    return ", ".join(bahia.nombre for bahia in bahias)
