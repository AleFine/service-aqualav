"""Operational panel and detailed reports (RF-033, RF-034).

RF-033 CA-01 is the sentence this whole module is shaped by: "los totales del
tablero coinciden con el reporte detallado". The classic way to fail it is to
write the panel as six aggregate queries and the reports as four listings, and
then spend a semester explaining why the dashboard says 41 and the report says
40. So there is exactly one calculation here, and it runs in one direction:

    the two raw lists  ->  the four reports  ->  the panel

:func:`tablero` does not query anything. It calls :func:`generar` for the four
reports and reads its numbers out of their ``totales``. They cannot disagree
because there is nothing to disagree with.

Three definitions carry the rest, and each of them is data-driven rather than
named:

* **a service was attended** when ``hora_fin_real`` is stamped, which is what
  the transition carrying ``marca_fin_servicio`` does (P3). No state is named
  anywhere in this module;
* **who worked it** is the assignment row while it still exists and, once the
  delivery deleted it, the operator snapshot INC-6 froze into the
  ``reserva.calificacion_habilitada`` event. That event is read back in bulk,
  which is the second load-bearing use of the log the MVP only ever wrote;
* **what a bay could have been busy for** comes from the calendar RF-018 moved
  into data, so a holiday does not turn into an idle Tuesday.

RF-033 flow 3a ("si el calculo excede el tiempo esperado, se muestra el ultimo
valor consolidado con la hora de corte") is honoured by always stamping the cut
-off: the calculation is synchronous and therefore always the latest
consolidated value, and ``generado_en`` says as of when. RF-034 flow 4a, the
one that really is asynchronous, lives in ``exportacion_service``.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import DatosInvalidos, RangoDemasiadoAmplio, detalle
from app.core.horario import (
    ZONA_LIMA,
    Calendario,
    a_lima,
    a_utc,
    ahora_utc,
    desde_bd,
    ventana_del_dia,
)
from app.models import (
    ESTADOS_PAGO_INGRESADO,
    MONEDA_PREDETERMINADA,
    RANGO_MAXIMO_DIAS,
    Pago,
    Reserva,
    TipoReporte,
)
from app.repositories import asignacion as asignacion_repo
from app.repositories import bahia as bahia_repo
from app.repositories import evento as evento_repo
from app.repositories import reporte as reporte_repo
from app.repositories import usuario as usuario_repo
from app.services import agenda_service, eventos

#: The permission that guards the panel and the reports (principle P5).
PERMISO_LEER_REPORTES = "reporte:leer"

#: Label used when a finished service has no operator on record. It happens:
#: a walk-in served at the counter never went through an assignment.
SIN_OPERARIO = "Sin operario"


# --------------------------------------------------------------------------
# The shape every report has
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Columna:
    """One column: its key in a row, its heading, and how to render it.

    ``tipo`` exists for the exporter, not for the screen: ``dinero`` means the
    value is integer cents (P6) and a CSV or a PDF has to print ``25.00``
    while the API keeps the integer.
    """

    clave: str
    titulo: str
    tipo: str = "texto"


@dataclass(frozen=True)
class Reporte:
    """A heading, some columns, some rows and their totals. Nothing else.

    Every report - and the audit trail of RF-036, which is exported the same
    way - is this. One shape means one exporter instead of five.
    """

    tipo: str
    titulo: str
    desde: date
    hasta: date
    columnas: tuple[Columna, ...]
    filas: list[dict[str, Any]] = field(default_factory=list)
    totales: dict[str, Any] = field(default_factory=dict)
    generado_en: datetime = field(default_factory=ahora_utc)


@dataclass(frozen=True)
class PuntoTendencia:
    """One day of the trend chart RF-033 asks for ("gráficos de tendencia")."""

    fecha: date
    servicios: int
    ingresos_centimos: int


@dataclass(frozen=True)
class Tablero:
    """The indicator cards of RF-033, every one of them read off a report."""

    desde: date
    hasta: date
    servicios_atendidos: int
    ingresos_centimos: int
    ticket_promedio_centimos: int
    ocupacion_porcentaje: float
    tiempo_promedio_min: int
    calificacion_media: float
    calificaciones: int
    operarios_activos: int
    moneda: str
    #: RF-033 flow 2a / CA-02: no data is not an error, it is a fact with a
    #: notice attached. Every number above is a zero and this says why.
    sin_datos: bool
    aviso: str | None
    #: RF-033 flow 3a: "con la hora de corte". Always the truth about when the
    #: numbers were taken, which is the only honest thing a cache can say too.
    generado_en: datetime
    tendencia: list[PuntoTendencia] = field(default_factory=list)


AVISO_SIN_DATOS = (
    "No hubo servicios atendidos ni cobros registrados en el periodo seleccionado. "
    "Los indicadores se muestran en cero."
)


# --------------------------------------------------------------------------
# The period
# --------------------------------------------------------------------------
def validar_rango(desde: date, hasta: date) -> None:
    """RF-034 flow 2a / CA-02 and RF-036 flow 3a: at most twelve months.

    The same refusal for both requirements because it is the same rule, and
    the message says the limit so the caller knows what to ask for instead.
    """
    if hasta < desde:
        raise DatosInvalidos(
            "La fecha final del rango es anterior a la inicial.",
            detalles=[detalle("hasta", "Debe ser igual o posterior a «desde».")],
        )
    dias = (hasta - desde).days
    if dias > RANGO_MAXIMO_DIAS:
        raise RangoDemasiadoAmplio(
            detalles=[
                detalle("desde", f"El rango solicitado abarca {dias + 1} días."),
                detalle("hasta", f"El máximo permitido es de {RANGO_MAXIMO_DIAS + 1} días."),
            ]
        )


def limites_utc(desde: date, hasta: date) -> tuple[datetime, datetime]:
    """``[desde 00:00, hasta+1 00:00)`` in Lima, expressed in UTC.

    Half-open and inclusive of ``hasta``: an administrator asking for "the 3rd"
    means the whole of the 3rd, and the shop's day is a Lima day (RN-07).
    """
    inicio = datetime.combine(desde, time.min, tzinfo=ZONA_LIMA)
    fin = datetime.combine(hasta + timedelta(days=1), time.min, tzinfo=ZONA_LIMA)
    return a_utc(inicio), a_utc(fin)


def _fechas(desde: date, hasta: date) -> list[date]:
    return [desde + timedelta(days=paso) for paso in range((hasta - desde).days + 1)]


def _dia_lima(momento: datetime | None) -> date | None:
    normalizado = desde_bd(momento)
    return a_lima(normalizado).date() if normalizado is not None else None


def _minutos(inicio: datetime | None, fin: datetime | None) -> int:
    """Whole minutes between two instants, never negative."""
    principio, final = desde_bd(inicio), desde_bd(fin)
    if principio is None or final is None:
        return 0
    return max(0, int((final - principio).total_seconds() // 60))


def _promedio(total: int, cantidad: int) -> int:
    return total // cantidad if cantidad else 0


def _porcentaje(parte: int, total: int) -> float:
    return round(parte * 100 / total, 2) if total else 0.0


# --------------------------------------------------------------------------
# Raw material: who worked each finished service
# --------------------------------------------------------------------------
def _operarios_de(db: Session, reservas: Sequence[Reserva]) -> dict[int, int | None]:
    """``{reserva_id: operario_id}`` for a batch of finished services.

    Two sources, in this order, and the order is the point:

    1. ``asignacion_servicio``, while the reservation is still holding its
       resources;
    2. the ``reserva.calificacion_habilitada`` event, for everything already
       delivered - the delivery is exactly what deletes the assignment, and
       INC-6 froze the operator into the event precisely so this question
       would still have an answer afterwards.

    Both are read in bulk: a report over a year must not become one query per
    service.
    """
    ids = [reserva.id for reserva in reservas]
    operarios: dict[int, int | None] = dict.fromkeys(ids, None)

    for fila in asignacion_repo.listar_por_reservas(db, ids):
        operarios[fila.reserva_id] = fila.operario_id

    faltantes = [reserva_id for reserva_id, valor in operarios.items() if valor is None]
    desde_eventos = evento_repo.mapa_por_accion(
        db, eventos.ENTIDAD_RESERVA, eventos.RESERVA_CALIFICACION_HABILITADA, faltantes
    )
    for reserva_id, datos in desde_eventos.items():
        valor = datos.get("operario_id")
        if isinstance(valor, int):
            operarios[reserva_id] = valor

    return operarios


# --------------------------------------------------------------------------
# RF-034: the four reports
# --------------------------------------------------------------------------
COLUMNAS_SERVICIOS = (
    Columna("codigo", "Reserva"),
    Columna("fecha", "Fecha"),
    Columna("servicio", "Servicio"),
    Columna("cliente", "Cliente"),
    Columna("placa", "Placa"),
    Columna("bahia", "Bahía"),
    Columna("operario", "Operario"),
    Columna("minutos_atencion", "Minutos de atención", "entero"),
    Columna("importe_centimos", "Importe", "dinero"),
    Columna("calificacion", "Calificación", "entero"),
)


def servicios(db: Session, desde: date, hasta: date) -> Reporte:
    """Every service finished in the period, one row each (RF-034).

    This is the report the other three are derived from, and the one the panel
    reads its service, time and rating figures off.
    """
    validar_rango(desde, hasta)
    inicio, fin = limites_utc(desde, hasta)

    atendidas = reporte_repo.listar_atendidas(db, inicio, fin)
    operarios = _operarios_de(db, atendidas)
    nombres = usuario_repo.listar_por_ids(db, operarios.values())

    filas: list[dict[str, Any]] = []
    for reserva in atendidas:
        operario_id = operarios.get(reserva.id)
        operario = nombres.get(operario_id) if operario_id is not None else None
        calificacion = reserva.calificacion
        filas.append(
            {
                "reserva_id": reserva.id,
                "codigo": reserva.codigo,
                "fecha": _dia_lima(reserva.hora_fin_real),
                "servicio": reserva.servicio.nombre if reserva.servicio else "",
                "cliente": reserva.usuario.nombre_completo if reserva.usuario else "",
                "placa": reserva.vehiculo.placa if reserva.vehiculo else "",
                "bahia_id": reserva.bahia_id,
                "bahia": reserva.bahia.nombre if reserva.bahia else "",
                "operario_id": operario_id,
                "operario": operario.nombre_completo if operario else SIN_OPERARIO,
                # Real attention time when the vehicle was checked in, the
                # booked block otherwise (a walk-in has no other anchor).
                "minutos_atencion": _minutos(reserva.hora_ingreso, reserva.hora_fin_real)
                or _minutos(reserva.inicio, reserva.fin),
                "minutos_bloque": _minutos(reserva.inicio, reserva.fin),
                "importe_centimos": reserva.monto_centimos,
                "moneda": reserva.moneda,
                "calificacion": calificacion.puntuacion if calificacion else None,
            }
        )

    puntuaciones = [fila["calificacion"] for fila in filas if fila["calificacion"] is not None]
    minutos = sum(fila["minutos_atencion"] for fila in filas)

    totales = {
        "servicios": len(filas),
        "minutos_atencion": minutos,
        "minutos_bloque": sum(fila["minutos_bloque"] for fila in filas),
        "minutos_promedio": _promedio(minutos, len(filas)),
        "importe_centimos": sum(fila["importe_centimos"] for fila in filas),
        "calificaciones": len(puntuaciones),
        # 0.0 when nobody rated. ``Calificable.calificacion_promedio`` answers
        # ``None`` there and is right to: a service nobody rated is not a
        # service everybody hated. The PANEL is the one place the requirement
        # asks for a zero (RF-033 CA-02), and the zero is unambiguous anyway -
        # the minimum score is one star, so an average of 0.0 can only mean
        # "nobody rated", which ``calificaciones`` states outright.
        "calificacion_media": (
            round(sum(puntuaciones) / len(puntuaciones), 2) if puntuaciones else 0.0
        ),
    }
    return Reporte(
        tipo=TipoReporte.SERVICIOS.value,
        titulo="Servicios atendidos",
        desde=desde,
        hasta=hasta,
        columnas=COLUMNAS_SERVICIOS,
        filas=filas,
        totales=totales,
    )


COLUMNAS_INGRESOS = (
    Columna("fecha", "Fecha"),
    Columna("codigo", "Reserva"),
    Columna("cliente", "Cliente"),
    Columna("medio", "Medio"),
    Columna("estado", "Estado"),
    Columna("monto_centimos", "Cobrado", "dinero"),
    Columna("reembolsado_centimos", "Devuelto", "dinero"),
    Columna("neto_centimos", "Neto", "dinero"),
)


def ingresos(db: Session, desde: date, hasta: date) -> Reporte:
    """Every payment taken in the period, gross, returned and net (RF-034).

    The net of one payment is ``saldo_centimos``, the column INC-4 keeps up to
    date on every reversal, so the report never re-adds refunds by hand. The
    panel reads its revenue and its average ticket off the totals here, which
    is what makes RF-034 CA-01 ("el total del CSV coincide con el de pantalla")
    true by construction rather than by coincidence.
    """
    validar_rango(desde, hasta)
    inicio, fin = limites_utc(desde, hasta)

    pagos: list[Pago] = reporte_repo.listar_pagos(db, inicio, fin, ESTADOS_PAGO_INGRESADO)

    filas: list[dict[str, Any]] = []
    for pago in pagos:
        reserva = pago.reserva
        filas.append(
            {
                "pago_id": pago.id,
                "fecha": _dia_lima(pago.registrado_en),
                "reserva_id": pago.reserva_id,
                "codigo": reserva.codigo if reserva else "",
                "cliente": reserva.usuario.nombre_completo if reserva and reserva.usuario else "",
                "medio": pago.medio,
                "modalidad": reserva.modalidad_pago if reserva else "",
                "estado": pago.estado,
                "monto_centimos": pago.monto_centimos,
                "reembolsado_centimos": max(0, pago.monto_centimos - pago.saldo_centimos),
                "neto_centimos": pago.saldo_centimos,
                "moneda": pago.moneda,
            }
        )

    neto = sum(fila["neto_centimos"] for fila in filas)
    totales = {
        "pagos": len(filas),
        "bruto_centimos": sum(fila["monto_centimos"] for fila in filas),
        "reembolsado_centimos": sum(fila["reembolsado_centimos"] for fila in filas),
        "neto_centimos": neto,
        # RF-033 "ticket promedio": what the shop takes per payment, net of
        # what it gave back. Derived from this report so the card and the
        # listing can never drift apart.
        "ticket_promedio_centimos": _promedio(neto, len(filas)),
    }
    return Reporte(
        tipo=TipoReporte.INGRESOS.value,
        titulo="Ingresos",
        desde=desde,
        hasta=hasta,
        columnas=COLUMNAS_INGRESOS,
        filas=filas,
        totales=totales,
    )


COLUMNAS_PRODUCTIVIDAD = (
    Columna("operario", "Operario"),
    Columna("servicios", "Servicios", "entero"),
    Columna("minutos_atencion", "Minutos de atención", "entero"),
    Columna("minutos_promedio", "Minutos por servicio", "entero"),
    Columna("calificaciones", "Calificaciones", "entero"),
    Columna("calificacion_media", "Calificación media", "decimal"),
)


def productividad(db: Session, desde: date, hasta: date) -> Reporte:
    """The service report grouped by operator (RF-034).

    Grouped in memory from :func:`servicios` rather than aggregated in SQL, on
    purpose: it guarantees the two reports count the same services, and the
    set is bounded by a twelve-month period that a shop with four bays fills
    at a very human rate.
    """
    base = servicios(db, desde, hasta)

    acumulado: dict[int | None, dict[str, Any]] = {}
    for fila in base.filas:
        clave = fila["operario_id"]
        grupo = acumulado.setdefault(
            clave,
            {
                "operario_id": clave,
                "operario": fila["operario"],
                "servicios": 0,
                "minutos_atencion": 0,
                "calificaciones": 0,
                "suma_calificaciones": 0,
            },
        )
        grupo["servicios"] += 1
        grupo["minutos_atencion"] += fila["minutos_atencion"]
        if fila["calificacion"] is not None:
            grupo["calificaciones"] += 1
            grupo["suma_calificaciones"] += fila["calificacion"]

    filas: list[dict[str, Any]] = []
    for grupo in acumulado.values():
        conteo = grupo.pop("suma_calificaciones")
        grupo["minutos_promedio"] = _promedio(grupo["minutos_atencion"], grupo["servicios"])
        grupo["calificacion_media"] = (
            round(conteo / grupo["calificaciones"], 2) if grupo["calificaciones"] else 0.0
        )
        filas.append(grupo)
    filas.sort(key=lambda grupo: (-grupo["servicios"], grupo["operario"]))

    totales = {
        "operarios": len(filas),
        "servicios": base.totales["servicios"],
        "minutos_atencion": base.totales["minutos_atencion"],
        "minutos_promedio": base.totales["minutos_promedio"],
    }
    return Reporte(
        tipo=TipoReporte.PRODUCTIVIDAD.value,
        titulo="Productividad por operario",
        desde=desde,
        hasta=hasta,
        columnas=COLUMNAS_PRODUCTIVIDAD,
        filas=filas,
        totales=totales,
    )


COLUMNAS_OCUPACION = (
    Columna("bahia", "Bahía"),
    Columna("servicios", "Servicios", "entero"),
    Columna("minutos_ocupados", "Minutos ocupados", "entero"),
    Columna("minutos_disponibles", "Minutos disponibles", "entero"),
    Columna("ocupacion_porcentaje", "Ocupación %", "decimal"),
)


def _minutos_disponibles(calendario: Calendario, fechas: Iterable[date]) -> int:
    """How long ONE bay could have been busy over those dates.

    Read from the calendar RF-018 moved into data, so a holiday or a closed
    weekday contributes zero instead of inflating the denominator and making
    a perfectly busy shop look idle.
    """
    total = 0
    for fecha in fechas:
        ventana = ventana_del_dia(fecha, calendario)
        if ventana is not None:
            apertura, cierre = ventana
            total += int((cierre - apertura).total_seconds() // 60)
    return total


def ocupacion(db: Session, desde: date, hasta: date) -> Reporte:
    """Minutes each bay was booked against the minutes it could have been (RF-034)."""
    base = servicios(db, desde, hasta)
    fechas = _fechas(desde, hasta)
    disponibles = _minutos_disponibles(agenda_service.calendario(db, desde, hasta), fechas)

    ocupados: dict[int, int] = {}
    conteo: dict[int, int] = {}
    for fila in base.filas:
        bahia_id = fila["bahia_id"]
        ocupados[bahia_id] = ocupados.get(bahia_id, 0) + fila["minutos_bloque"]
        conteo[bahia_id] = conteo.get(bahia_id, 0) + 1

    filas: list[dict[str, Any]] = []
    for bahia in bahia_repo.listar_todas(db):
        minutos = ocupados.get(bahia.id, 0)
        if not bahia.activa and not minutos:
            # A bay that is switched off and did nothing is not a zero to
            # explain; it is simply not part of the shop this period.
            continue
        filas.append(
            {
                "bahia_id": bahia.id,
                "bahia": bahia.nombre,
                "servicios": conteo.get(bahia.id, 0),
                "minutos_ocupados": minutos,
                "minutos_disponibles": disponibles,
                "ocupacion_porcentaje": _porcentaje(minutos, disponibles),
            }
        )

    total_ocupados = sum(fila["minutos_ocupados"] for fila in filas)
    total_disponibles = disponibles * len(filas)
    totales = {
        "bahias": len(filas),
        "servicios": base.totales["servicios"],
        "minutos_ocupados": total_ocupados,
        "minutos_disponibles": total_disponibles,
        "ocupacion_porcentaje": _porcentaje(total_ocupados, total_disponibles),
    }
    return Reporte(
        tipo=TipoReporte.OCUPACION.value,
        titulo="Ocupación por bahía",
        desde=desde,
        hasta=hasta,
        columnas=COLUMNAS_OCUPACION,
        filas=filas,
        totales=totales,
    )


#: Report type -> the function that builds it. A dispatch table and not an
#: ``if`` chain, so the API and the scheduler agree on what a type is and
#: adding a fifth report is one entry. The audit trail of RF-036 is exportable
#: too, but it is NOT registered here: it would be an import cycle, and
#: ``exportacion_service`` composes the two dispatch tables explicitly instead
#: of relying on somebody importing a module for its side effect.
CONSTRUCTORES: dict[str, Any] = {
    TipoReporte.SERVICIOS.value: servicios,
    TipoReporte.INGRESOS.value: ingresos,
    TipoReporte.PRODUCTIVIDAD.value: productividad,
    TipoReporte.OCUPACION.value: ocupacion,
}


def generar(db: Session, tipo: str, desde: date, hasta: date, **filtros: Any) -> Reporte:
    """Build the report of ``tipo``. Unknown types are a 422, never a KeyError."""
    constructor = CONSTRUCTORES.get(tipo)
    if constructor is None:
        raise DatosInvalidos(
            "No existe ese tipo de reporte.",
            detalles=[detalle("tipo", f"Tipos válidos: {', '.join(sorted(CONSTRUCTORES))}.")],
        )
    return constructor(db, desde, hasta, **filtros)


# --------------------------------------------------------------------------
# RF-033: the panel, read off the reports above
# --------------------------------------------------------------------------
def _tendencia(reporte_servicios: Reporte, reporte_ingresos: Reporte) -> list[PuntoTendencia]:
    """One point per day: services finished and money taken (RF-033 "tendencia").

    Derived from the very same rows the cards are, so a chart can never tell a
    different story from the number above it.
    """
    por_dia: dict[date, list[int]] = {}
    for fila in reporte_servicios.filas:
        por_dia.setdefault(fila["fecha"], [0, 0])[0] += 1
    for fila in reporte_ingresos.filas:
        por_dia.setdefault(fila["fecha"], [0, 0])[1] += fila["neto_centimos"]

    return [
        PuntoTendencia(fecha=fecha, servicios=valores[0], ingresos_centimos=valores[1])
        for fecha, valores in sorted(por_dia.items())
        if fecha is not None
    ]


def tablero(db: Session, desde: date, hasta: date) -> Tablero:
    """The six indicators of RF-033, every one of them taken from a report.

    CA-01 is not tested against this function so much as guaranteed by it:
    there is no arithmetic here that is not already in a report's ``totales``.

    CA-02 - "periodo sin servicios -> valores en cero sin error" - falls out of
    the same structure: an empty list sums to zero, averages guard their
    divisor, and the only thing left to add is the notice flow 2a asks for.
    """
    validar_rango(desde, hasta)

    reporte_servicios = servicios(db, desde, hasta)
    reporte_ingresos = ingresos(db, desde, hasta)
    reporte_productividad = productividad(db, desde, hasta)
    reporte_ocupacion = ocupacion(db, desde, hasta)

    sin_datos = not reporte_servicios.filas and not reporte_ingresos.filas

    return Tablero(
        desde=desde,
        hasta=hasta,
        servicios_atendidos=reporte_servicios.totales["servicios"],
        ingresos_centimos=reporte_ingresos.totales["neto_centimos"],
        ticket_promedio_centimos=reporte_ingresos.totales["ticket_promedio_centimos"],
        ocupacion_porcentaje=reporte_ocupacion.totales["ocupacion_porcentaje"],
        tiempo_promedio_min=reporte_servicios.totales["minutos_promedio"],
        calificacion_media=reporte_servicios.totales["calificacion_media"],
        calificaciones=reporte_servicios.totales["calificaciones"],
        operarios_activos=reporte_productividad.totales["operarios"],
        moneda=MONEDA_PREDETERMINADA,
        sin_datos=sin_datos,
        aviso=AVISO_SIN_DATOS if sin_datos else None,
        generado_en=reporte_servicios.generado_en,
        tendencia=_tendencia(reporte_servicios, reporte_ingresos),
    )
