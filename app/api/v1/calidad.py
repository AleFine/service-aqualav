"""Evidence and rating of a service (RF-023, RF-031, RN-10).

Both hang off the reservation, and both reuse its horizontal authorization:
``reserva_service.obtener`` answers 404 for somebody else's booking, so asking
for somebody else's photographs or somebody else's rating answers 404 too,
through the same line, for the same reason (RF-017 CA-03).

Reading needs no permission of its own - either reading permission of a
reservation reaches them, exactly as the receipt of RF-027 does. Writing does:
``evidencia:registrar`` belongs to the shop (the two actors RF-023 names) and
``calificacion:crear`` to the customer (the one actor RF-031 names).
"""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.deps import get_db, permisos_actuales, requiere_algun_permiso, requiere_permiso
from app.models import Usuario
from app.schemas import (
    CalificacionEstadoOut,
    CalificacionIn,
    CalificacionOut,
    ErrorBody,
    EvidenciaIn,
    EvidenciaOut,
    Lista,
)
from app.services import calificacion_service, evidencia_service, reserva_service
from app.services.ensamblador import (
    armar_calificacion,
    armar_estado_calificacion,
    armar_evidencia,
    armar_evidencias,
)

router = APIRouter(prefix="/reservas", tags=["calidad"])

#: Same rule as the receipt: reading the evidence or the rating of a service is
#: reading the service. A permission of its own would only be a second place to
#: get the horizontal filter wrong.
LECTURA = ("reserva:leer_propias", "reserva:leer_todas")

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


# --------------------------------------------------------------------------
# RF-023 - evidence
# --------------------------------------------------------------------------
@router.get(
    "/{reserva_id}/evidencias",
    response_model=Lista[EvidenciaOut],
    responses=RESPUESTAS,
    summary="Fotografías de evidencia del servicio",
)
def listar_evidencias(
    reserva_id: int,
    autor: Usuario = Depends(requiere_algun_permiso(*LECTURA)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> Lista[EvidenciaOut]:
    """RF-023 `CA-01`: «el cliente abre su servicio y puede visualizarla»."""
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    return Lista[EvidenciaOut](items=armar_evidencias(evidencia_service.listar(db, reserva)))


@router.post(
    "/{reserva_id}/evidencias",
    response_model=EvidenciaOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {
            "model": EvidenciaOut,
            "description": "Reintento de una evidencia ya registrada (RF-023 `4a`).",
        },
        **RESPUESTAS,
    },
    summary="Registrar una fotografía de evidencia",
)
def registrar_evidencia(
    reserva_id: int,
    datos: EvidenciaIn,
    respuesta: Response,
    autor: Usuario = Depends(requiere_permiso("evidencia:registrar")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> EvidenciaOut:
    """RF-023. 201 la primera vez; 200 cuando el dispositivo reintenta.

    El cuerpo puede llegar **sin** `contenido_base64`: eso deja la evidencia
    `pendiente`, que es el flujo `4a`. Enviando la misma `referencia_cliente`
    con el contenido, la carga se completa sin crear una segunda fotografía
    (`CA-02`, «al recuperar la conexión se sube sin intervención»).
    """
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    evidencia, creada = evidencia_service.registrar(db, reserva, datos, autor)
    if not creada:
        respuesta.status_code = status.HTTP_200_OK
    return armar_evidencia(evidencia)


# --------------------------------------------------------------------------
# RF-031 / RN-10 - rating
# --------------------------------------------------------------------------
@router.get(
    "/{reserva_id}/calificacion",
    response_model=CalificacionEstadoOut,
    responses=RESPUESTAS,
    summary="Estado de la calificación del servicio",
)
def estado_calificacion(
    reserva_id: int,
    autor: Usuario = Depends(requiere_algun_permiso(*LECTURA)),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> CalificacionEstadoOut:
    """RF-031 + `RN-10`: si se puede calificar, hasta cuándo, y qué se dijo ya.

    Responde siempre 200: «todavía no se entregó», «el plazo venció» (`3a`) y
    «ya calificaste, en modo lectura» (`3b`) son estados de la pantalla, no
    errores, y el campo `motivo` trae el texto listo para mostrar.
    """
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    return armar_estado_calificacion(calificacion_service.ventana(db, reserva))


@router.post(
    "/{reserva_id}/calificacion",
    response_model=CalificacionOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {
            "model": CalificacionOut,
            "description": "Ya existía: se devuelve en modo lectura (RF-031 `3b`).",
        },
        **RESPUESTAS,
    },
    summary="Calificar un servicio entregado",
)
def calificar(
    reserva_id: int,
    datos: CalificacionIn,
    respuesta: Response,
    autor: Usuario = Depends(requiere_permiso("calificacion:crear")),
    permisos: list[str] = Depends(permisos_actuales),
    db: Session = Depends(get_db),
) -> CalificacionOut:
    """RF-031. 201 la primera vez; 200 devolviendo la existente (`3b`).

    Fuera de los 7 días calendario de `RN-10` responde `422
    PLAZO_DE_CALIFICACION_VENCIDO` con la fecha en que venció; sobre un
    servicio que aún no se entregó, `422 CALIFICACION_NO_HABILITADA`.
    """
    reserva = reserva_service.obtener(db, reserva_id, autor, permisos)
    calificacion, creada = calificacion_service.calificar(db, reserva, autor, datos)
    if not creada:
        respuesta.status_code = status.HTTP_200_OK
    return armar_calificacion(calificacion)
