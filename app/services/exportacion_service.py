"""Report exports, synchronous and asynchronous (RF-034, RF-036).

One exporter for five reports, because a report is always the same thing: a
heading, some columns, some rows and their totals
(:class:`~app.services.reporte_service.Reporte`). Writing a CSV writer per
report type is how the CSV total stops matching the screen, which is precisely
what RF-034 CA-01 forbids - so the totals a file carries are the ``totales``
dict the API answered with, not a second sum taken over the written rows.

**Nothing new is written to produce the PDF.** INC-4 built a real, hand-made
PDF 1.4 generator with no dependency and exposed
``ProveedorDocumentos.reporte(titulo, lineas)`` for exactly this moment. Its
one page holds :data:`~app.services.proveedores.documentos.LINEAS_MAXIMAS`
lines, which is why :func:`_lineas_pdf` prints the TOTALS FIRST and the detail
afterwards: if a long report does not fit, what survives is the part CA-01 is
about, and the file says how many rows it had to leave out and where to get
them. A CSV has no such limit and carries everything.

Flow 4a - "volumen elevado -> exportacion asincrona con notificacion al
finalizar" - is a row and a sweep. Over
``settings.reporte_umbral_filas`` rows the request is accepted and left
``pendiente``; ``planificador.ejecutar_pendientes`` generates the file and
tells whoever asked, through a template and an event like every other notice
in the system (INC-5: "para notificar algo nuevo, añade plantilla y evento, no
escribas un despacho a mano"). Nothing here builds a message.
"""

import codecs
import csv
import io
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import (
    DatosInvalidos,
    ExportacionNoDisponible,
    PermisoDenegado,
    RecursoNoEncontrado,
    detalle,
)
from app.core.horario import a_lima, ahora_utc, desde_bd
from app.models import (
    EstadoExportacion,
    EventoNotificacion,
    FormatoReporte,
    ReporteExportacion,
    TipoReporte,
    Usuario,
)
from app.repositories import reporte as reporte_repo
from app.services import auditoria_service, eventos, notificacion_service, reporte_service
from app.services.proveedores.almacenamiento import (
    ObjetoNoEncontrado,
    ProveedorAlmacenamiento,
    proveedor_almacenamiento,
)
from app.services.proveedores.documentos import (
    LINEAS_MAXIMAS,
    MIME_PDF,
    ProveedorDocumentos,
    proveedor_documentos,
)

logger = logging.getLogger("aqualav.exportaciones")

MIME_CSV = "text/csv; charset=utf-8"

#: Report type -> builder, composed explicitly from the two modules that own
#: them. ``auditoria_service`` is not registered inside ``reporte_service``
#: because it imports it; composing here keeps the dependency one-way and
#: keeps the table from depending on somebody importing a module for its side
#: effects.
CONSTRUCTORES: dict[str, Any] = {
    **reporte_service.CONSTRUCTORES,
    TipoReporte.AUDITORIA.value: auditoria_service.reporte,
}

#: Which permission each type demands. The audit trail is not a sales report
#: and must not be readable with the reporting permission (RF-036 actors are
#: the administrator and the system). Validated IN THE SERVICE, which is the
#: pattern INC-7 established: an operation reachable through more than one
#: door checks its own permission.
PERMISOS_POR_TIPO: dict[str, str] = {
    TipoReporte.AUDITORIA.value: auditoria_service.PERMISO_LEER_AUDITORIA,
}

#: Spanish headings for the total keys the reports produce. One table for all
#: of them, so a file never prints a raw identifier at a human.
ETIQUETAS_TOTALES: dict[str, str] = {
    "servicios": "Servicios atendidos",
    "minutos_atencion": "Minutos de atención",
    "minutos_bloque": "Minutos reservados",
    "minutos_promedio": "Minutos por servicio",
    "importe_centimos": "Importe facturado",
    "calificaciones": "Calificaciones",
    "calificacion_media": "Calificación media",
    "pagos": "Pagos registrados",
    "bruto_centimos": "Cobrado",
    "reembolsado_centimos": "Devuelto",
    "neto_centimos": "Ingresos netos",
    "ticket_promedio_centimos": "Ticket promedio",
    "operarios": "Operarios con servicios",
    "bahias": "Bahías",
    "minutos_ocupados": "Minutos ocupados",
    "minutos_disponibles": "Minutos disponibles",
    "ocupacion_porcentaje": "Ocupación %",
    "registros": "Registros en el periodo",
    "exportados": "Registros exportados",
}

FORMATO_FECHA = "%d/%m/%Y"
FORMATO_FECHA_HORA = "%d/%m/%Y %H:%M"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _dinero(centimos: int) -> str:
    """``2500`` -> ``25.00``. Integer cents in, text out (P6)."""
    signo = "-" if centimos < 0 else ""
    return f"{signo}{abs(centimos) // 100}.{abs(centimos) % 100:02d}"


def formatear(valor: Any, tipo: str) -> str:
    """One cell, rendered for a human. ``None`` is an empty cell, never "None"."""
    if valor is None:
        return ""
    if tipo == "dinero" and isinstance(valor, int):
        return _dinero(valor)
    if tipo == "decimal" and isinstance(valor, int | float):
        return f"{float(valor):.2f}"
    if isinstance(valor, date | datetime):
        return valor.strftime(FORMATO_FECHA)
    return str(valor)


def _tipo_de_total(clave: str) -> str:
    """How to print a total, inferred from its key.

    ``*_centimos`` is money and ``*_porcentaje`` or ``*_media`` is a decimal.
    A convention rather than a third table to keep in sync: the keys already
    say what they are, and a report that invents a new one prints as text
    instead of printing wrong.
    """
    if clave.endswith("_centimos"):
        return "dinero"
    if clave.endswith(("_porcentaje", "_media")):
        return "decimal"
    return "texto"


def _totales_legibles(reporte: reporte_service.Reporte) -> list[tuple[str, str]]:
    return [
        (ETIQUETAS_TOTALES.get(clave, clave), formatear(valor, _tipo_de_total(clave)))
        for clave, valor in reporte.totales.items()
    ]


def a_csv(reporte: reporte_service.Reporte) -> bytes:
    """The whole report as CSV (RF-034 CA-01).

    Encoded UTF-8 with a BOM so the shop's spreadsheet opens «Bahía» correctly
    instead of showing mojibake, which is the difference between a report
    somebody uses and a report somebody complains about.

    The totals block at the end carries the API's own ``totales``: the number
    in the file and the number on the screen are the same object, not two sums
    of the same rows.
    """
    memoria = io.StringIO(newline="")
    escritor = csv.writer(memoria, delimiter=",", lineterminator="\r\n")

    escritor.writerow([columna.titulo for columna in reporte.columnas])
    for fila in reporte.filas:
        escritor.writerow(
            [formatear(fila.get(columna.clave), columna.tipo) for columna in reporte.columnas]
        )

    escritor.writerow([])
    escritor.writerow(["Totales"])
    for etiqueta, valor in _totales_legibles(reporte):
        escritor.writerow([etiqueta, valor])

    return codecs.BOM_UTF8 + memoria.getvalue().encode("utf-8")


def _lineas_pdf(reporte: reporte_service.Reporte) -> list[str]:
    """The PDF body: period, totals, and as much detail as one page holds.

    Totals before detail on purpose. The hand-written generator INC-4 built
    renders a single page, so a long report WILL be cut; what must survive the
    cut is the figure RF-034 CA-01 compares against the screen, and the note
    at the end tells the reader the CSV has the rest.
    """
    lineas = [
        f"Periodo: {reporte.desde.strftime(FORMATO_FECHA)} "
        f"- {reporte.hasta.strftime(FORMATO_FECHA)}",
        f"Generado: {a_lima(reporte.generado_en).strftime(FORMATO_FECHA_HORA)}",
        "",
        "Totales",
    ]
    lineas.extend(f"  {etiqueta}: {valor}" for etiqueta, valor in _totales_legibles(reporte))
    lineas.extend(["", "Detalle", "  " + " | ".join(c.titulo for c in reporte.columnas)])

    # One line is kept back for the truncation notice, so the notice can never
    # be the line that gets dropped.
    disponibles = max(0, LINEAS_MAXIMAS - len(lineas) - 1)
    for fila in reporte.filas[:disponibles]:
        lineas.append(
            "  " + " | ".join(formatear(fila.get(c.clave), c.tipo) for c in reporte.columnas)
        )

    if len(reporte.filas) > disponibles:
        lineas.append(
            f"  Se muestran {disponibles} de {len(reporte.filas)} filas. "
            "Exporta a CSV para el detalle completo."
        )
    return lineas


def renderizar(
    reporte: reporte_service.Reporte,
    formato: str,
    *,
    documentos: ProveedorDocumentos | None = None,
) -> tuple[bytes, str]:
    """``(bytes, mime)`` of the report in ``formato``."""
    if formato == FormatoReporte.CSV.value:
        return a_csv(reporte), MIME_CSV
    if formato == FormatoReporte.PDF.value:
        generador = documentos or proveedor_documentos()
        titulo = f"AquaLav - {reporte.titulo}"
        return generador.reporte(titulo, _lineas_pdf(reporte)), MIME_PDF
    raise DatosInvalidos(
        "No existe ese formato de exportación.",
        detalles=[detalle("formato", "Formatos válidos: csv, pdf.")],
    )


# --------------------------------------------------------------------------
# Building a report from a request
# --------------------------------------------------------------------------
def _validar_permiso(tipo: str, permisos: list[str]) -> None:
    requerido = PERMISOS_POR_TIPO.get(tipo, reporte_service.PERMISO_LEER_REPORTES)
    if requerido not in set(permisos or ()):
        raise PermisoDenegado(detalles=[detalle(None, f"Se requiere el permiso {requerido}.")])


def construir(db: Session, tipo: str, desde: date, hasta: date, **filtros: Any) -> Any:
    """Build any of the five reports. Unknown types are a 422, not a KeyError."""
    constructor = CONSTRUCTORES.get(tipo)
    if constructor is None:
        raise DatosInvalidos(
            "No existe ese tipo de reporte.",
            detalles=[detalle("tipo", f"Tipos válidos: {', '.join(sorted(CONSTRUCTORES))}.")],
        )
    limpios = {clave: valor for clave, valor in filtros.items() if valor is not None}
    return constructor(db, desde, hasta, **limpios)


def clave_de_archivo(fila: ReporteExportacion) -> str:
    """Where the file lives in the object store. A KEY, never a path."""
    return f"reportes/{fila.tipo}/{fila.id:08d}.{fila.formato}"


def _filtros_de(fila: ReporteExportacion) -> dict[str, Any]:
    """Rebuild the call that produced the report, from the row alone.

    This is why ``filtros`` is persisted: the sweep runs hours later, with no
    request and no session of the person who asked, and it still has to build
    exactly the report they asked for.
    """
    guardados = dict(fila.filtros or {})
    guardados.pop("desde", None)
    guardados.pop("hasta", None)
    return guardados


def _periodo_de(fila: ReporteExportacion) -> tuple[date, date]:
    guardados = dict(fila.filtros or {})
    return (
        date.fromisoformat(str(guardados["desde"])),
        date.fromisoformat(str(guardados["hasta"])),
    )


# --------------------------------------------------------------------------
# The lifecycle of one export
# --------------------------------------------------------------------------
def _materializar(
    db: Session,
    fila: ReporteExportacion,
    reporte: Any,
    *,
    momento: datetime,
    documentos: ProveedorDocumentos | None = None,
    almacenamiento: ProveedorAlmacenamiento | None = None,
) -> ReporteExportacion:
    """Render the file, store it and close the row. Never raises."""
    almacen = almacenamiento or proveedor_almacenamiento()
    try:
        contenido, mime = renderizar(reporte, fila.formato, documentos=documentos)
        clave = clave_de_archivo(fila)
        almacen.guardar(clave, contenido, mime)
    except Exception as error:  # noqa: BLE001 - a failed export is a row, not a 500
        logger.warning("No se pudo generar la exportación %s: %s", fila.id, error)
        reporte_repo.cerrar_exportacion(
            db,
            fila,
            estado=EstadoExportacion.FALLIDO.value,
            momento=momento,
            error=str(error)[:300],
        )
        eventos.registrar_evento(
            db,
            eventos.ENTIDAD_REPORTE,
            fila.id,
            eventos.REPORTE_EXPORTACION_FALLIDA,
            autor_id=fila.solicitado_por_id,
            datos={"tipo": fila.tipo, "formato": fila.formato, "causa": str(error)[:300]},
        )
        return fila

    reporte_repo.cerrar_exportacion(
        db,
        fila,
        estado=EstadoExportacion.GENERADO.value,
        momento=momento,
        archivo_key=clave,
        filas=len(reporte.filas),
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_REPORTE,
        fila.id,
        eventos.REPORTE_EXPORTACION_GENERADA,
        autor_id=fila.solicitado_por_id,
        datos={
            "tipo": fila.tipo,
            "formato": fila.formato,
            "filas": fila.filas,
            "archivo_key": clave,
        },
    )
    return fila


def solicitar(
    db: Session,
    *,
    tipo: str,
    formato: str,
    desde: date,
    hasta: date,
    autor: Usuario,
    permisos: list[str],
    filtros: dict[str, Any] | None = None,
    momento: datetime | None = None,
    documentos: ProveedorDocumentos | None = None,
    almacenamiento: ProveedorAlmacenamiento | None = None,
) -> ReporteExportacion:
    """Ask for an export (RF-034, RF-036 "exportable").

    Small reports are produced inside the request: the person clicked and the
    file is there. Over ``settings.reporte_umbral_filas`` rows the request is
    ACCEPTED and left pending, which is flow 4a - answering "aquí lo tienes" is
    honest only while producing it is instant.

    The report is built before that decision is taken, because the row count is
    what the decision is about. The pending path then throws that build away
    and the sweep repeats it; paying one extra build on the rare heavy request
    buys a rule that never has to guess how big a period is going to be.
    """
    _validar_permiso(tipo, permisos)
    momento = momento or ahora_utc()
    argumentos = dict(filtros or {})

    reporte = construir(db, tipo, desde, hasta, **argumentos)

    fila = reporte_repo.crear_exportacion(
        db,
        tipo=tipo,
        formato=formato,
        filtros={**argumentos, "desde": desde.isoformat(), "hasta": hasta.isoformat()},
        estado=EstadoExportacion.PENDIENTE.value,
        solicitado_por_id=autor.id,
        solicitado_en=momento,
    )
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_REPORTE,
        fila.id,
        eventos.REPORTE_EXPORTACION_SOLICITADA,
        autor_id=autor.id,
        datos={
            "tipo": tipo,
            "formato": formato,
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
            "filas": len(reporte.filas),
        },
    )

    if len(reporte.filas) <= settings.reporte_umbral_filas:
        _materializar(
            db,
            fila,
            reporte,
            momento=momento,
            documentos=documentos,
            almacenamiento=almacenamiento,
        )

    db.commit()
    db.refresh(fila)
    return fila


def procesar_pendientes(
    db: Session,
    momento: datetime | None = None,
    *,
    documentos: ProveedorDocumentos | None = None,
    almacenamiento: ProveedorAlmacenamiento | None = None,
    **proveedores: Any,
) -> list[int]:
    """Generate the queued exports and tell whoever asked (RF-034 flow 4a).

    Called by the scheduler, which is the pure function the tests call too, so
    "asynchronous" never means "you have to wait for a real clock".

    The notice is ``notificacion_service.despachar`` with the ``exportacion``
    event: a template and a row, exactly as INC-5 asked. Nothing here writes a
    message, picks a channel or knows the customer's language.
    """
    momento = momento or ahora_utc()
    generadas: list[int] = []

    for fila in reporte_repo.listar_pendientes(db, EstadoExportacion.PENDIENTE.value):
        try:
            desde, hasta = _periodo_de(fila)
            reporte = construir(db, fila.tipo, desde, hasta, **_filtros_de(fila))
        except Exception as error:  # noqa: BLE001 - a bad row must not stop the sweep
            logger.warning("Exportación %s no reconstruible: %s", fila.id, error)
            reporte_repo.cerrar_exportacion(
                db,
                fila,
                estado=EstadoExportacion.FALLIDO.value,
                momento=momento,
                error=str(error)[:300],
            )
            continue

        _materializar(
            db,
            fila,
            reporte,
            momento=momento,
            documentos=documentos,
            almacenamiento=almacenamiento,
        )
        generadas.append(fila.id)

        solicitante = fila.solicitado_por
        if solicitante is not None:
            notificacion_service.despachar(
                db,
                solicitante,
                EventoNotificacion.EXPORTACION.value,
                datos={
                    "reporte": fila.tipo,
                    "formato": fila.formato,
                    "estado": fila.estado,
                    "filas": fila.filas,
                    "archivo": fila.nombre_archivo,
                },
                momento=momento,
                **proveedores,
            )

    return generadas


# --------------------------------------------------------------------------
# Reading exports back
# --------------------------------------------------------------------------
def obtener(db: Session, exportacion_id: int) -> ReporteExportacion:
    fila = reporte_repo.obtener_exportacion(db, exportacion_id)
    if fila is None:
        raise RecursoNoEncontrado("No encontramos esa exportación.")
    return fila


def listar(db: Session, *, solicitado_por_id: int | None = None) -> list[ReporteExportacion]:
    return reporte_repo.listar_exportaciones(db, solicitado_por_id=solicitado_por_id)


def archivo(
    db: Session,
    fila: ReporteExportacion,
    *,
    almacenamiento: ProveedorAlmacenamiento | None = None,
) -> tuple[bytes, str]:
    """``(bytes, mime)`` of a finished export."""
    if not fila.archivo_key:
        raise ExportacionNoDisponible(
            detalles=[detalle("estado", f"La exportación está «{fila.estado}».")]
        )
    almacen = almacenamiento or proveedor_almacenamiento()
    try:
        contenido = almacen.leer(fila.archivo_key)
    except ObjetoNoEncontrado as error:
        raise RecursoNoEncontrado(
            "El archivo de la exportación ya no está disponible. Vuelve a solicitarla.",
            detalles=[detalle("archivo_key", str(error))],
        ) from error
    mime = MIME_PDF if fila.formato == FormatoReporte.PDF.value else MIME_CSV
    return contenido, mime


def momento_de(fila: ReporteExportacion) -> datetime | None:
    """The generation instant, normalised for the API layer."""
    return desde_bd(fila.generado_en)
