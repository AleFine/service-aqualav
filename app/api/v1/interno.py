"""Internal operations a scheduled process would run (RF-030, plan section 4).

The simulated scheduler is a pure function, and this is the door that calls it
on demand: a demo, a cron entry or an operator can force the sweep that the
background loop performs on its own. It is not a debug hatch - it is the one
supported way to run the sweep when the loop is switched off, and it is gated
by a permission of its own.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import ErrorBody, PlanificadorOut
from app.services import planificador

router = APIRouter(prefix="/interno", tags=["interno"])

#: Principle P5: who may force a sweep is a permission, never a role name.
PERMISO_PLANIFICADOR = "planificador:ejecutar"

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
}


@router.post(
    "/planificador",
    response_model=PlanificadorOut,
    responses=RESPUESTAS,
    summary=(
        "Ejecutar el barrido del planificador "
        "(recordatorios, caducidad de pago, cola y exportaciones)"
    ),
)
def ejecutar_planificador(
    _: Usuario = Depends(requiere_permiso(PERMISO_PLANIFICADOR)),
    db: Session = Depends(get_db),
) -> PlanificadorOut:
    """RF-030, RF-014 `2a` y RF-034 `4a`.

    Recuerda, caduca los pagos vencidos, promueve la cola y genera las
    exportaciones encoladas notificando a quien las pidió.
    """
    resultado = planificador.ejecutar_pendientes(db)
    return PlanificadorOut(
        momento=resultado.momento,
        recordatorios_enviados=len(resultado.recordatorios),
        reservas_promovidas=resultado.promovidas,
        reservas_expiradas=resultado.expiradas,
        exportaciones_generadas=resultado.exportaciones,
    )
