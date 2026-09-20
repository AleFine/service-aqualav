"""Operative agenda: opening calendar, blockings and the board (RF-018, RN-07).

This is the module that finally gives ``app.core.horario.es_laborable`` an
answer worth asking for. The MVP kept RN-07 as three constants and returned a
constant ``True``; v1.0 reads the week from ``horario_atencion``, the holidays
from ``dia_no_laborable`` and the shop-wide closures from ``bloqueo_franja``,
folds them into a pure :class:`~app.core.horario.Calendario` and hands it to
whoever needs it. ``app/core`` never opens a session.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.core.errors import DatosInvalidos, FranjaConReservas, RecursoNoEncontrado, detalle
from app.core.horario import (
    TRAMOS_RN07,
    ZONA_LIMA,
    Calendario,
    a_lima,
    a_utc,
    ahora,
    desde_bd,
    ventana_del_dia,
)
from app.models import BloqueoFranja, DiaNoLaborable, HorarioAtencion, Usuario
from app.repositories import agenda as agenda_repo
from app.repositories import asignacion as asignacion_repo
from app.repositories import bahia as bahia_repo
from app.repositories import reserva as reserva_repo
from app.repositories import transicion as transicion_repo
from app.schemas import (
    VISTA_SEMANA,
    AgendaBahiaOut,
    AgendaDiaOut,
    AgendaOut,
    AgendaReservaOut,
    BahiaResumen,
    BloqueoIn,
    BloqueoOut,
    DiaNoLaborableIn,
    HorarioAtencionIn,
    HorarioAtencionOut,
)
from app.services import eventos

#: Permissions that guard this module (principle P5).
PERMISO_LEER = "agenda:leer"
PERMISO_ADMINISTRAR = "agenda:administrar"

#: Days a weekly view spans (RF-018: "vista diaria y semanal").
DIAS_DE_LA_SEMANA = 7


@dataclass(frozen=True)
class Franja:
    """One blocked interval, normalized to aware UTC. ``bahia_id`` None = all bays."""

    bahia_id: int | None
    inicio: datetime
    fin: datetime

    def afecta(self, bahia_id: int, inicio: datetime, fin: datetime) -> bool:
        """Whether this blocking hits ``[inicio, fin)`` of that bay (RF-018 CA-01)."""
        if self.bahia_id is not None and self.bahia_id != bahia_id:
            return False
        return self.inicio < fin and self.fin > inicio


def _limites_utc(desde: date, hasta: date) -> tuple[datetime, datetime]:
    """The ``[00:00 of desde, 00:00 of hasta+1)`` window, in UTC."""
    inicio = datetime.combine(desde, time.min, tzinfo=ZONA_LIMA)
    fin = datetime.combine(hasta + timedelta(days=1), time.min, tzinfo=ZONA_LIMA)
    return a_utc(inicio), a_utc(fin)


def _tramos_vigentes(db: Session, referencia: date) -> dict[int, tuple[time, time]]:
    """The opening window of each weekday in force on ``referencia``.

    Rows come back oldest vigency first, so the last one that applies wins. A
    weekday whose row carries no hours is CLOSED and drops out of the mapping.
    An empty table falls back to the literal RN-07 week, so the API keeps
    working even if the reference data was never seeded.
    """
    filas = agenda_repo.listar_horarios_vigentes(db, referencia)
    if not filas:
        return dict(TRAMOS_RN07)

    tramos: dict[int, tuple[time, time]] = {}
    for fila in filas:
        if fila.hora_apertura is None or fila.hora_cierre is None:
            tramos.pop(fila.dia_semana, None)
        else:
            tramos[fila.dia_semana] = (fila.hora_apertura, fila.hora_cierre)
    return tramos


def calendario(db: Session, desde: date, hasta: date | None = None) -> Calendario:
    """Build the opening calendar that covers ``[desde, hasta]`` (RF-018 CA-01).

    The weekly windows are resolved as of ``desde``: a vigency that starts
    INSIDE the queried window applies from the next query, which is accurate
    for every caller (they all query from today or from the reserved date).
    """
    hasta = hasta or desde
    tramos = _tramos_vigentes(db, desde)
    feriados: dict[date, str] = {
        fila.fecha: fila.motivo for fila in agenda_repo.listar_dias_no_laborables(db, desde, hasta)
    }

    base = Calendario(tramos=tramos, feriados=dict(feriados))
    inicio_utc, fin_utc = _limites_utc(desde, hasta)
    totales = [
        bloqueo
        for bloqueo in agenda_repo.listar_bloqueos(db, inicio_utc, fin_utc)
        if bloqueo.bahia_id is None
    ]

    # A shop-wide blocking that swallows a whole opening window is, in effect,
    # a non working day: ``es_laborable`` has to say so (RF-018 CA-01).
    fecha = desde
    while fecha <= hasta:
        ventana = ventana_del_dia(fecha, base)
        if ventana is not None:
            apertura, cierre = ventana
            for bloqueo in totales:
                inicio = desde_bd(bloqueo.inicio)
                fin = desde_bd(bloqueo.fin)
                if inicio <= a_utc(apertura) and fin >= a_utc(cierre):
                    feriados[fecha] = bloqueo.descripcion or bloqueo.motivo
                    break
        fecha += timedelta(days=1)

    return Calendario(tramos=tramos, feriados=feriados)


def franjas_bloqueadas(db: Session, desde: date, hasta: date | None = None) -> list[Franja]:
    """Blockings covering ``[desde, hasta]``, ready for the availability filter."""
    hasta = hasta or desde
    inicio_utc, fin_utc = _limites_utc(desde, hasta)
    return [
        Franja(
            bahia_id=bloqueo.bahia_id,
            inicio=desde_bd(bloqueo.inicio),
            fin=desde_bd(bloqueo.fin),
        )
        for bloqueo in agenda_repo.listar_bloqueos(db, inicio_utc, fin_utc)
    ]


def bahias_bloqueadas(
    franjas: list[Franja],
    ids_bahias: list[int],
    inicio: datetime,
    fin: datetime,
) -> set[int]:
    """Subset of ``ids_bahias`` a blocking takes out of ``[inicio, fin)``."""
    inicio_utc = a_utc(inicio)
    fin_utc = a_utc(fin)
    return {
        bahia_id
        for bahia_id in ids_bahias
        if any(franja.afecta(bahia_id, inicio_utc, fin_utc) for franja in franjas)
    }


# --------------------------------------------------------------------------
# The board (RF-018: daily and weekly view, by bay)
# --------------------------------------------------------------------------
def _rango(fecha: date, vista: str) -> tuple[date, date]:
    if vista != VISTA_SEMANA:
        return fecha, fecha
    lunes = fecha - timedelta(days=fecha.weekday())
    return lunes, lunes + timedelta(days=DIAS_DE_LA_SEMANA - 1)


def agenda(db: Session, fecha: date, vista: str) -> AgendaOut:
    """The board the counter and the administrator work from (RF-018)."""
    desde, hasta = _rango(fecha, vista)
    calendario_vigente = calendario(db, desde, hasta)
    estados_activos = transicion_repo.listar_estados_no_terminales(db)
    bahias = bahia_repo.listar_todas(db)

    inicio_utc, fin_utc = _limites_utc(desde, hasta)
    reservas = reserva_repo.listar_en_rango(db, inicio_utc, fin_utc, estados_activos)
    asignaciones = {
        fila.reserva_id: fila
        for fila in asignacion_repo.listar_por_reservas(db, [r.id for r in reservas])
    }
    bloqueos = agenda_repo.listar_bloqueos(db, inicio_utc, fin_utc)

    dias: list[AgendaDiaOut] = []
    actual = desde
    while actual <= hasta:
        ventana = ventana_del_dia(actual, calendario_vigente)
        dia_inicio, dia_fin = _limites_utc(actual, actual)
        dias.append(
            AgendaDiaOut(
                fecha=actual,
                laborable=ventana is not None,
                motivo_no_laborable=calendario_vigente.motivo_no_laborable(actual),
                apertura=ventana[0] if ventana else None,
                cierre=ventana[1] if ventana else None,
                bahias=[
                    AgendaBahiaOut(
                        bahia=BahiaResumen.model_validate(bahia),
                        estado=bahia.estado,
                        reservas=[
                            _armar_agenda_reserva(reserva, asignaciones.get(reserva.id))
                            for reserva in reservas
                            if reserva.bahia_id == bahia.id
                            and desde_bd(reserva.inicio) < dia_fin
                            and desde_bd(reserva.fin) > dia_inicio
                        ],
                        bloqueos=[
                            _armar_bloqueo(bloqueo)
                            for bloqueo in bloqueos
                            if (bloqueo.bahia_id in (None, bahia.id))
                            and desde_bd(bloqueo.inicio) < dia_fin
                            and desde_bd(bloqueo.fin) > dia_inicio
                        ],
                    )
                    for bahia in bahias
                ],
            )
        )
        actual += timedelta(days=1)

    return AgendaOut(vista=vista, desde=desde, hasta=hasta, dias=dias)


def _armar_agenda_reserva(reserva, asignacion) -> AgendaReservaOut:
    return AgendaReservaOut(
        reserva_id=reserva.id,
        codigo=reserva.codigo,
        estado=reserva.estado,
        inicio=a_lima(desde_bd(reserva.inicio)),
        fin=a_lima(desde_bd(reserva.fin)),
        cliente=reserva.usuario.nombre_completo,
        placa=reserva.vehiculo.placa,
        servicio=reserva.servicio.nombre,
        operario=asignacion.operario.nombre_completo if asignacion is not None else None,
    )


def _armar_bloqueo(bloqueo: BloqueoFranja) -> BloqueoOut:
    return BloqueoOut(
        id=bloqueo.id,
        bahia=BahiaResumen.model_validate(bloqueo.bahia) if bloqueo.bahia else None,
        inicio=a_lima(desde_bd(bloqueo.inicio)),
        fin=a_lima(desde_bd(bloqueo.fin)),
        motivo=bloqueo.motivo,
        descripcion=bloqueo.descripcion,
        autor=bloqueo.autor,
    )


# --------------------------------------------------------------------------
# Blockings (RF-018 flow 4a / CA-02)
# --------------------------------------------------------------------------
def listar_bloqueos(db: Session, desde: date, hasta: date | None = None) -> list[BloqueoFranja]:
    inicio_utc, fin_utc = _limites_utc(desde, hasta or desde)
    return agenda_repo.listar_bloqueos(db, inicio_utc, fin_utc)


def crear_bloqueo(db: Session, datos: BloqueoIn, autor: Usuario) -> BloqueoFranja:
    """Block a slot, refusing to do it over live bookings (flow 4a / CA-02).

    The refusal is the requirement: the shop has to RESOLVE those reservations
    - reschedule or cancel them - before the slot disappears, because applying
    the blocking silently would leave customers with a place that is gone.
    """
    inicio = a_utc(a_lima(datos.inicio))
    fin = a_utc(a_lima(datos.fin))
    if fin <= inicio:
        raise DatosInvalidos(
            "El fin del bloqueo debe ser posterior a su inicio.",
            detalles=[detalle("fin", "Revisa el inicio y el fin de la franja.")],
        )

    if datos.bahia_id is not None:
        bahia = bahia_repo.obtener_por_id(db, datos.bahia_id)
        if bahia is None:
            raise RecursoNoEncontrado(
                "No encontramos esa bahía.",
                detalles=[detalle("bahia_id", "La bahía no existe.")],
            )

    estados_activos = transicion_repo.listar_estados_no_terminales(db)
    afectadas = reserva_repo.listar_activas_de_bahia(
        db, datos.bahia_id, inicio, fin, estados_activos
    )
    if afectadas:
        raise FranjaConReservas(
            detalles=[
                detalle(
                    "reservas",
                    f"{reserva.codigo} ({a_lima(desde_bd(reserva.inicio)).isoformat()}).",
                )
                for reserva in afectadas
            ]
        )

    bloqueo = agenda_repo.crear_bloqueo(
        db,
        bahia_id=datos.bahia_id,
        inicio=inicio,
        fin=fin,
        motivo=datos.motivo.value,
        descripcion=(datos.descripcion or "").strip() or None,
        autor_id=autor.id,
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_AGENDA,
        bloqueo.id,
        eventos.AGENDA_BLOQUEO_CREADO,
        autor_id=autor.id,
        datos={
            "bahia_id": datos.bahia_id,
            "inicio": inicio.isoformat(),
            "fin": fin.isoformat(),
            "motivo": bloqueo.motivo,
        },
    )
    db.commit()
    db.refresh(bloqueo)
    return bloqueo


def eliminar_bloqueo(db: Session, bloqueo_id: int, autor: Usuario) -> None:
    bloqueo = agenda_repo.obtener_bloqueo(db, bloqueo_id)
    if bloqueo is None:
        raise RecursoNoEncontrado("No encontramos ese bloqueo.")

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_AGENDA,
        bloqueo.id,
        eventos.AGENDA_BLOQUEO_ELIMINADO,
        autor_id=autor.id,
        datos={"bahia_id": bloqueo.bahia_id, "motivo": bloqueo.motivo},
    )
    agenda_repo.eliminar_bloqueo(db, bloqueo)
    db.commit()


# --------------------------------------------------------------------------
# Holidays (RN-07 completed)
# --------------------------------------------------------------------------
def listar_dias_no_laborables(
    db: Session, desde: date | None = None, hasta: date | None = None
) -> list[DiaNoLaborable]:
    return agenda_repo.listar_dias_no_laborables(db, desde, hasta)


def crear_dia_no_laborable(db: Session, datos: DiaNoLaborableIn, autor: Usuario) -> DiaNoLaborable:
    """Declare a date the shop does not open (RF-018, RN-07 + holidays).

    Live bookings on that date are refused the same way a blocking is: the
    shop resolves them first.
    """
    existente = agenda_repo.obtener_dia_no_laborable_por_fecha(db, datos.fecha)
    if existente is not None:
        return existente

    inicio_utc, fin_utc = _limites_utc(datos.fecha, datos.fecha)
    estados_activos = transicion_repo.listar_estados_no_terminales(db)
    afectadas = reserva_repo.listar_activas_de_bahia(db, None, inicio_utc, fin_utc, estados_activos)
    if afectadas:
        raise FranjaConReservas(
            "El día que quieres marcar como no laborable tiene reservas activas. "
            "Reubícalas o cancélalas antes de cerrarlo.",
            detalles=[detalle("reservas", reserva.codigo) for reserva in afectadas],
        )

    fila = agenda_repo.crear_dia_no_laborable(
        db, fecha=datos.fecha, motivo=datos.motivo, autor_id=autor.id
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_AGENDA,
        fila.id,
        eventos.AGENDA_DIA_NO_LABORABLE_CREADO,
        autor_id=autor.id,
        datos={"fecha": fila.fecha.isoformat(), "motivo": fila.motivo},
    )
    db.commit()
    db.refresh(fila)
    return fila


def eliminar_dia_no_laborable(db: Session, dia_id: int, autor: Usuario) -> None:
    fila = agenda_repo.obtener_dia_no_laborable(db, dia_id)
    if fila is None:
        raise RecursoNoEncontrado("No encontramos ese día no laborable.")

    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_AGENDA,
        fila.id,
        eventos.AGENDA_DIA_NO_LABORABLE_ELIMINADO,
        autor_id=autor.id,
        datos={"fecha": fila.fecha.isoformat()},
    )
    agenda_repo.eliminar_dia_no_laborable(db, fila)
    db.commit()


# --------------------------------------------------------------------------
# Opening hours (RN-07 as data)
# --------------------------------------------------------------------------
def listar_horarios(db: Session, referencia: date | None = None) -> list[HorarioAtencionOut]:
    """The opening window of every weekday in force on ``referencia``.

    Always seven entries, closed days included, so the screen can render the
    whole week without guessing which weekday is missing.
    """
    fecha = referencia or a_lima(ahora()).date()
    tramos = _tramos_vigentes(db, fecha)
    filas = {fila.dia_semana: fila for fila in agenda_repo.listar_horarios_vigentes(db, fecha)}
    return [
        HorarioAtencionOut(
            dia_semana=dia,
            hora_apertura=tramos.get(dia, (None, None))[0],
            hora_cierre=tramos.get(dia, (None, None))[1],
            vigente_desde=filas[dia].vigente_desde if dia in filas else fecha,
            cerrado=dia not in tramos,
        )
        for dia in range(DIAS_DE_LA_SEMANA)
    ]


def definir_horario(
    db: Session, dia_semana: int, datos: HorarioAtencionIn, autor: Usuario
) -> HorarioAtencion:
    """Open a new vigency for one weekday. Both hours absent closes that day."""
    vigente_desde = datos.vigente_desde or a_lima(ahora()).date()
    fila = agenda_repo.crear_horario(
        db,
        dia_semana=dia_semana,
        hora_apertura=datos.hora_apertura,
        hora_cierre=datos.hora_cierre,
        vigente_desde=vigente_desde,
        autor_id=autor.id,
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_AGENDA,
        fila.id,
        eventos.AGENDA_HORARIO_ACTUALIZADO,
        autor_id=autor.id,
        datos={
            "dia_semana": dia_semana,
            "hora_apertura": datos.hora_apertura.isoformat() if datos.hora_apertura else None,
            "hora_cierre": datos.hora_cierre.isoformat() if datos.hora_cierre else None,
            "vigente_desde": vigente_desde.isoformat(),
        },
    )
    db.commit()
    db.refresh(fila)
    return fila
