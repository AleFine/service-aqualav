"""Operational panel, detailed reports and their exports (RF-033, RF-034).

Four doors:

* ``GET /reportes/tablero`` - the indicator cards of RF-033;
* ``GET /reportes/{tipo}`` - the detailed report behind each card (RF-034);
* ``POST /reportes/exportaciones`` - ask for a CSV or a PDF. Answers **201**
  when the file is ready and **202** when the volume sent it to the scheduler
  (RF-034 flow 4a);
* ``GET /reportes/exportaciones[/{id}][/archivo]`` - the requests and the file.

The export door also serves the audit trail of RF-036, which is exportable
too. It is therefore reachable with either reading permission, and WHICH one
each report type demands is decided inside ``exportacion_service`` - the
pattern INC-7 established: an operation reachable through more than one door
validates its permission in the service, not only in its router.

That applies to ALL FOUR export doors, not just to the one that creates the
file: asking for an export, listing the requests, reading one back and
downloading its bytes each carry the caller's permissions into the service,
and the service decides on the ``tipo`` of the row in hand. The listing also
filters by requester, because an export row remembers the filters it was
asked with and those say what somebody was looking into (RF-034, RF-036,
RNF-014).
"""

from datetime import date

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import Response as RespuestaBinaria
from sqlalchemy.orm import Session

from app.deps import get_db, permisos_actuales, requiere_algun_permiso, requiere_permiso
from app.models import Usuario
from app.schemas import ErrorBody, ExportacionIn, ExportacionOut, Lista, ReporteOut, TableroOut
from app.services import auditoria_service, exportacion_service, reporte_service
from app.services.ensamblador import armar_exportacion, armar_reporte, armar_tablero

router = APIRouter(prefix="/reportes", tags=["reportes"])

#: Either reading permission opens the export resource; the service decides
#: which one the requested TYPE actually needs.
LECTURA = (reporte_service.PERMISO_LEER_REPORTES, auditoria_service.PERMISO_LEER_AUDITORIA)

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.get(
    "/tablero",
    response_model=TableroOut,
    responses=RESPUESTAS,
    summary="Tablero de indicadores operativos",
)
def tablero(
    desde: date = Query(description="Primer día del periodo, inclusive."),
    hasta: date = Query(description="Último día del periodo, inclusive."),
    _: Usuario = Depends(requiere_permiso(reporte_service.PERMISO_LEER_REPORTES)),
    db: Session = Depends(get_db),
) -> TableroOut:
    """RF-033: servicios atendidos, ingresos, ocupación, ticket promedio,
    tiempo promedio de atención y calificación media del periodo.

    `CA-01`: cada cifra se lee del reporte detallado correspondiente, así que
    coinciden por construcción. `CA-02`: un periodo sin servicios responde 200
    con todos los indicadores en cero y el aviso del flujo `2a`.
    """
    return armar_tablero(reporte_service.tablero(db, desde, hasta))


# --------------------------------------------------------------------------
# Exports. Declared BEFORE ``/{tipo}`` so the literal path wins the match.
# --------------------------------------------------------------------------
@router.post(
    "/exportaciones",
    response_model=ExportacionOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        202: {
            "model": ExportacionOut,
            "description": "Volumen elevado: se generará de forma asíncrona (RF-034 4a).",
        },
        **RESPUESTAS,
    },
    summary="Exportar un reporte a CSV o PDF",
)
def exportar(
    datos: ExportacionIn,
    respuesta: Response,
    autor: Usuario = Depends(requiere_algun_permiso(*LECTURA)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ExportacionOut:
    """RF-034 `CA-01`: el total del archivo coincide con el de pantalla.
    `CA-02` / `2a`: un rango mayor a 12 meses se rechaza con un mensaje que
    explica el límite. `4a`: si el volumen es alto se responde **202** y el
    planificador genera el archivo y notifica al solicitante.
    """
    fila = exportacion_service.solicitar(
        db,
        tipo=datos.tipo.value,
        formato=datos.formato.value,
        desde=datos.desde,
        hasta=datos.hasta,
        autor=autor,
        permisos=permisos,
        filtros={
            "usuario_id": datos.usuario_id,
            "accion": datos.accion,
            "entidad": datos.entidad,
            "entidad_id": datos.entidad_id,
        },
    )
    if fila.archivo_key is None:
        respuesta.status_code = status.HTTP_202_ACCEPTED
    return armar_exportacion(fila)


@router.get(
    "/exportaciones",
    response_model=Lista[ExportacionOut],
    responses=RESPUESTAS,
    summary="Listar las exportaciones solicitadas",
)
def listar_exportaciones(
    autor: Usuario = Depends(requiere_algun_permiso(*LECTURA)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> Lista[ExportacionOut]:
    """Las exportaciones **propias** cuyo tipo el solicitante puede leer.

    Dos filtros y no uno: el solicitante (filtro horizontal, el mismo que
    aplica ``GET /reservas``) y el permiso del tipo de cada fila (filtro
    vertical). El primero impide ver qué estuvo investigando otro
    administrador —una fila de exportación guarda los filtros con los que se
    pidió—; el segundo impide que un rol con solo ``reporte:leer`` vea las
    exportaciones de auditoría. Ninguno de los dos hace innecesario al otro.
    """
    return Lista[ExportacionOut](
        items=[
            armar_exportacion(fila)
            for fila in exportacion_service.listar(
                db, permisos=permisos, solicitado_por_id=autor.id
            )
        ]
    )


@router.get(
    "/exportaciones/{exportacion_id}",
    response_model=ExportacionOut,
    responses=RESPUESTAS,
    summary="Consultar el estado de una exportación",
)
def obtener_exportacion(
    exportacion_id: int,
    _: Usuario = Depends(requiere_algun_permiso(*LECTURA)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> ExportacionOut:
    """RF-034 `4a`: mientras el estado sea «pendiente» no hay archivo todavía.

    El permiso que exige el **tipo de la fila** se valida en el servicio: la
    puerta admite cualquiera de los dos permisos de lectura y solo la fila
    sabe cuál de los dos hace falta de verdad.
    """
    return armar_exportacion(exportacion_service.obtener(db, exportacion_id, permisos=permisos))


@router.get(
    "/exportaciones/{exportacion_id}/archivo",
    responses={
        200: {"content": {"text/csv": {}, "application/pdf": {}}},
        **RESPUESTAS,
    },
    summary="Descargar el archivo de una exportación",
)
def descargar_exportacion(
    exportacion_id: int,
    _: Usuario = Depends(requiere_algun_permiso(*LECTURA)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> RespuestaBinaria:
    """RF-034: el CSV o el PDF generado. 409 si todavía no está listo.

    RNF-014: descargar la bitácora exige ``auditoria:leer`` aunque la puerta
    se abra también con ``reporte:leer``.
    """
    fila = exportacion_service.obtener(db, exportacion_id, permisos=permisos)
    contenido, mime = exportacion_service.archivo(db, fila, permisos=permisos)
    return RespuestaBinaria(
        content=contenido,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{fila.nombre_archivo}"'},
    )


@router.get(
    "/{tipo}",
    response_model=ReporteOut,
    responses=RESPUESTAS,
    summary="Reporte detallado de servicios, ingresos, productividad u ocupación",
)
def reporte(
    tipo: str,
    desde: date = Query(description="Primer día del periodo, inclusive."),
    hasta: date = Query(description="Último día del periodo, inclusive."),
    _: Usuario = Depends(requiere_permiso(reporte_service.PERMISO_LEER_REPORTES)),
    db: Session = Depends(get_db),
) -> ReporteOut:
    """RF-034: los cuatro reportes, con sus filas y sus totales.

    Los totales son el mismo diccionario que lee el tablero y que imprime el
    CSV, que es lo que hace cierto el `CA-01` de RF-033 y el de RF-034 sin
    que nadie tenga que sumar dos veces.
    """
    return armar_reporte(reporte_service.generar(db, tipo, desde, hasta))
