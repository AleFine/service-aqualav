"""Availability of time blocks (RF-013).

The algorithm is the one the contract fixes, in this exact order:

1. resolve the day's opening window (RN-07);
2. generate candidate starts every 15 minutes such that the whole service fits
   before closing time;
3. drop candidates earlier than ``now + 60 min`` (RN-02, RF-013 CA-02);
4. for each candidate, count the active bays with no overlapping reservation
   (RN-03) and offer it when at least one is free (CA-01);
5. when the requested date offers nothing, scan forward up to 14 days to
   suggest the next date with room (flow 3a).
"""

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.errors import RecursoNoEncontrado
from app.core.horario import (
    PASO_BLOQUE_MIN,
    a_lima,
    a_utc,
    ahora,
    desde_bd,
    es_laborable,
    ventana_del_dia,
)
from app.models import Servicio
from app.repositories import bahia as bahia_repo
from app.repositories import reserva as reserva_repo
from app.repositories import transicion as transicion_repo
from app.schemas import BloqueDisponible, DisponibilidadOut
from app.services import servicio_service

#: RN-02: a reservation needs at least one hour of lead time.
ANTICIPACION_MINIMA = timedelta(minutes=60)

#: RF-013 flow 3a: how far ahead to look for the next date with room.
DIAS_BUSQUEDA_SIGUIENTE = 14


def _ocupacion(
    db: Session,
    apertura: datetime,
    cierre: datetime,
    estados_activos: set[str],
) -> list[tuple[int, datetime, datetime]]:
    """Active reservations touching the day, normalized to aware UTC."""
    filas = reserva_repo.listar_ocupacion(db, a_utc(apertura), a_utc(cierre), estados_activos)
    return [(bahia_id, desde_bd(inicio), desde_bd(fin)) for bahia_id, inicio, fin in filas]


def _bahias_libres(
    ids_bahias: list[int],
    ocupacion: list[tuple[int, datetime, datetime]],
    inicio: datetime,
    fin: datetime,
) -> int:
    """How many bays have no reservation overlapping ``[inicio, fin)`` (RN-03)."""
    inicio_utc = a_utc(inicio)
    fin_utc = a_utc(fin)
    ocupadas = {
        bahia_id
        for bahia_id, ocupado_inicio, ocupado_fin in ocupacion
        if ocupado_inicio < fin_utc and ocupado_fin > inicio_utc
    }
    return sum(1 for bahia_id in ids_bahias if bahia_id not in ocupadas)


def calcular_bloques(
    db: Session,
    servicio: Servicio,
    fecha: date,
    ids_bahias: list[int],
    momento: datetime,
    *,
    estados_activos: set[str] | None = None,
) -> list[BloqueDisponible]:
    """Free blocks of one date. ``momento`` is "now", used by the RN-02 filter.

    ``estados_activos`` is the set of states that still hold a bay, read from
    ``transicion_estado`` (P3). Callers that sweep several dates resolve it ONCE
    and pass it in: this function runs up to 43 times per day and up to fourteen
    days per query, and one extra query per call would cost RNF-002.
    """
    ventana = ventana_del_dia(fecha)
    if ventana is None or not ids_bahias:
        return []

    if estados_activos is None:
        estados_activos = transicion_repo.listar_estados_no_terminales(db)

    apertura, cierre = ventana
    duracion = timedelta(minutes=servicio.duracion_min)
    paso = timedelta(minutes=PASO_BLOQUE_MIN)
    minimo = a_lima(momento) + ANTICIPACION_MINIMA

    ocupacion = _ocupacion(db, apertura, cierre, estados_activos)

    bloques: list[BloqueDisponible] = []
    inicio = apertura
    while inicio + duracion <= cierre:
        if inicio >= minimo:
            libres = _bahias_libres(ids_bahias, ocupacion, inicio, inicio + duracion)
            if libres > 0:
                bloques.append(
                    BloqueDisponible(inicio=inicio, fin=inicio + duracion, bahias_libres=libres)
                )
        inicio += paso

    return bloques


def _siguiente_fecha(
    db: Session,
    servicio: Servicio,
    fecha: date,
    ids_bahias: list[int],
    momento: datetime,
    estados_activos: set[str],
) -> date | None:
    """First date after ``fecha`` with at least one free block (flow 3a)."""
    for dias in range(1, DIAS_BUSQUEDA_SIGUIENTE + 1):
        candidata = fecha + timedelta(days=dias)
        if calcular_bloques(
            db, servicio, candidata, ids_bahias, momento, estados_activos=estados_activos
        ):
            return candidata
    return None


def consultar(db: Session, fecha: date, servicio_id: int) -> DisponibilidadOut:
    """Answer ``GET /disponibilidad`` for one date and one service."""
    servicio = servicio_service.obtener_publico(db, servicio_id)
    if servicio.duracion_min <= 0:  # pragma: no cover - guarded by the schema
        raise RecursoNoEncontrado("El servicio no tiene una duración válida.")

    ids_bahias = [bahia.id for bahia in bahia_repo.listar_activas(db)]
    momento = ahora()
    # Resolved once and threaded through the whole sweep (up to fifteen dates).
    estados_activos = transicion_repo.listar_estados_no_terminales(db)

    bloques = calcular_bloques(
        db, servicio, fecha, ids_bahias, momento, estados_activos=estados_activos
    )
    siguiente = None
    if not bloques:
        siguiente = _siguiente_fecha(db, servicio, fecha, ids_bahias, momento, estados_activos)

    return DisponibilidadOut(
        fecha=fecha,
        laborable=es_laborable(fecha),
        servicio_id=servicio.id,
        duracion_min=servicio.duracion_min,
        bloques=bloques,
        siguiente_fecha_disponible=siguiente,
    )


def bloques_cercanos(
    db: Session,
    servicio: Servicio,
    referencia: datetime,
    limite: int = 3,
) -> list[BloqueDisponible]:
    """Free blocks closest to a taken one, to fill the 409 of RF-014 flow 3a."""
    referencia_lima = a_lima(referencia)
    bloques = calcular_bloques(
        db,
        servicio,
        referencia_lima.date(),
        [bahia.id for bahia in bahia_repo.listar_activas(db)],
        ahora(),
        estados_activos=transicion_repo.listar_estados_no_terminales(db),
    )
    bloques.sort(key=lambda bloque: abs(a_lima(bloque.inicio) - referencia_lima))
    return bloques[:limite]
