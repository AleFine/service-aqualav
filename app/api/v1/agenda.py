"""Operative agenda endpoints (RF-018).

Reading the board is guarded by ``agenda:leer`` and every mutation by
``agenda:administrar``; RF-018 names the administrator AND the receptionist as
its actors, and both hold those codes after migration ``0004``.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.deps import get_db, requiere_permiso
from app.models import Usuario
from app.schemas import (
    VISTA_DIA,
    VISTAS,
    AgendaOut,
    BloqueoIn,
    BloqueoOut,
    DiaNoLaborableIn,
    DiaNoLaborableOut,
    ErrorBody,
    HorarioAtencionIn,
    HorarioAtencionOut,
    Lista,
)
from app.services import agenda_service
from app.services.ensamblador import armar_dia_no_laborable

router = APIRouter(prefix="/agenda", tags=["agenda"])

RESPUESTAS = {
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


@router.get(
    "",
    response_model=AgendaOut,
    responses=RESPUESTAS,
    summary="Agenda diaria o semanal por bahía",
)
def ver(
    fecha: date = Query(description="Fecha a mostrar, en formato YYYY-MM-DD (hora de Lima)."),
    vista: str = Query(default=VISTA_DIA, pattern="^(dia|semana)$"),
    _: Usuario = Depends(requiere_permiso(agenda_service.PERMISO_LEER)),
    db: Session = Depends(get_db),
) -> AgendaOut:
    return agenda_service.agenda(db, fecha, vista if vista in VISTAS else VISTA_DIA)


@router.get(
    "/horarios",
    response_model=Lista[HorarioAtencionOut],
    responses=RESPUESTAS,
    summary="Horario de atención vigente, por día de la semana",
)
def listar_horarios(
    _: Usuario = Depends(requiere_permiso(agenda_service.PERMISO_LEER)),
    db: Session = Depends(get_db),
) -> Lista[HorarioAtencionOut]:
    return Lista[HorarioAtencionOut](items=agenda_service.listar_horarios(db))


@router.put(
    "/horarios/{dia_semana}",
    response_model=HorarioAtencionOut,
    responses=RESPUESTAS,
    summary="Abrir una nueva vigencia del horario de un día",
)
def definir_horario(
    dia_semana: int,
    datos: HorarioAtencionIn,
    autor: Usuario = Depends(requiere_permiso(agenda_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> HorarioAtencionOut:
    fila = agenda_service.definir_horario(db, dia_semana, datos, autor)
    return HorarioAtencionOut(
        dia_semana=fila.dia_semana,
        hora_apertura=fila.hora_apertura,
        hora_cierre=fila.hora_cierre,
        vigente_desde=fila.vigente_desde,
        cerrado=fila.hora_apertura is None,
    )


@router.get(
    "/dias-no-laborables",
    response_model=Lista[DiaNoLaborableOut],
    responses=RESPUESTAS,
    summary="Feriados y días de cierre declarados",
)
def listar_dias_no_laborables(
    desde: date | None = Query(default=None),
    hasta: date | None = Query(default=None),
    _: Usuario = Depends(requiere_permiso(agenda_service.PERMISO_LEER)),
    db: Session = Depends(get_db),
) -> Lista[DiaNoLaborableOut]:
    filas = agenda_service.listar_dias_no_laborables(db, desde, hasta)
    return Lista[DiaNoLaborableOut](items=[armar_dia_no_laborable(fila) for fila in filas])


@router.post(
    "/dias-no-laborables",
    response_model=DiaNoLaborableOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Declarar un día no laborable",
)
def crear_dia_no_laborable(
    datos: DiaNoLaborableIn,
    autor: Usuario = Depends(requiere_permiso(agenda_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> DiaNoLaborableOut:
    return armar_dia_no_laborable(agenda_service.crear_dia_no_laborable(db, datos, autor))


@router.delete(
    "/dias-no-laborables/{dia_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=RESPUESTAS,
    summary="Volver a abrir un día no laborable",
)
def eliminar_dia_no_laborable(
    dia_id: int,
    autor: Usuario = Depends(requiere_permiso(agenda_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> None:
    agenda_service.eliminar_dia_no_laborable(db, dia_id, autor)


@router.get(
    "/bloqueos",
    response_model=Lista[BloqueoOut],
    responses=RESPUESTAS,
    summary="Franjas bloqueadas de un rango",
)
def listar_bloqueos(
    desde: date = Query(description="Primer día del rango (YYYY-MM-DD)."),
    hasta: date | None = Query(default=None),
    _: Usuario = Depends(requiere_permiso(agenda_service.PERMISO_LEER)),
    db: Session = Depends(get_db),
) -> Lista[BloqueoOut]:
    filas = agenda_service.listar_bloqueos(db, desde, hasta)
    return Lista[BloqueoOut](items=[BloqueoOut.model_validate(fila) for fila in filas])


@router.post(
    "/bloqueos",
    response_model=BloqueoOut,
    status_code=status.HTTP_201_CREATED,
    responses=RESPUESTAS,
    summary="Bloquear una franja de una bahía o de todo el local",
)
def crear_bloqueo(
    datos: BloqueoIn,
    autor: Usuario = Depends(requiere_permiso(agenda_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> BloqueoOut:
    return BloqueoOut.model_validate(agenda_service.crear_bloqueo(db, datos, autor))


@router.delete(
    "/bloqueos/{bloqueo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=RESPUESTAS,
    summary="Levantar un bloqueo",
)
def eliminar_bloqueo(
    bloqueo_id: int,
    autor: Usuario = Depends(requiere_permiso(agenda_service.PERMISO_ADMINISTRAR)),
    db: Session = Depends(get_db),
) -> None:
    agenda_service.eliminar_bloqueo(db, bloqueo_id, autor)
