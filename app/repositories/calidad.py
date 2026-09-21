"""Data access for ``evidencia`` and ``calificacion`` (RF-023, RF-031).

No rules here, as always: how many photographs a moment admits, whether the
rating window is still open and what an empty aggregate means are decisions,
and decisions live in the service layer.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Calificacion, Evidencia


# --------------------------------------------------------------------------
# evidencia (RF-023)
# --------------------------------------------------------------------------
def listar_evidencias(db: Session, reserva_id: int) -> list[Evidencia]:
    """Every photograph of one service, oldest first (CA-01)."""
    consulta = select(Evidencia).where(Evidencia.reserva_id == reserva_id).order_by(Evidencia.id)
    return list(db.scalars(consulta).all())


def obtener_evidencia(db: Session, evidencia_id: int) -> Evidencia | None:
    return db.get(Evidencia, evidencia_id)


def obtener_por_referencia(
    db: Session, reserva_id: int, referencia_cliente: str
) -> Evidencia | None:
    """The row the device already opened for this photograph (flow 4a).

    This lookup is what makes the automatic retry of CA-02 idempotent: the
    same call, replayed when the connection comes back, finds its own row and
    completes it instead of adding a duplicate.
    """
    consulta = select(Evidencia).where(
        Evidencia.reserva_id == reserva_id,
        Evidencia.referencia_cliente == referencia_cliente,
    )
    return db.scalars(consulta).first()


def contar_por_momento(db: Session, reserva_id: int, momento: str) -> int:
    """How many photographs this service already has of that moment."""
    return int(
        db.scalar(
            select(func.count(Evidencia.id)).where(
                Evidencia.reserva_id == reserva_id, Evidencia.momento == momento
            )
        )
        or 0
    )


def crear_evidencia(
    db: Session,
    *,
    reserva_id: int,
    momento: str,
    autor_id: int,
    estado_carga: str,
    observacion: str | None,
    referencia_cliente: str | None,
    registrada_en: datetime,
) -> Evidencia:
    fila = Evidencia(
        reserva_id=reserva_id,
        momento=momento,
        autor_id=autor_id,
        estado_carga=estado_carga,
        observacion=observacion,
        referencia_cliente=referencia_cliente,
        registrada_en=registrada_en,
    )
    db.add(fila)
    db.flush()
    return fila


def anotar_carga(
    db: Session,
    fila: Evidencia,
    *,
    estado_carga: str,
    objeto_key: str | None = None,
    mime: str | None = None,
    tamano_bytes: int | None = None,
    subida_en: datetime | None = None,
    error: str | None = None,
) -> Evidencia:
    """Write down how the last upload attempt of ``fila`` ended.

    ``intentos`` only ever goes up: it is the count of tries the device made,
    and it is what tells a screen that a photograph is stuck rather than slow.
    """
    fila.estado_carga = estado_carga
    fila.intentos += 1
    fila.error = error
    if objeto_key is not None:
        fila.objeto_key = objeto_key
    if mime is not None:
        fila.mime = mime
    if tamano_bytes is not None:
        fila.tamano_bytes = tamano_bytes
    if subida_en is not None:
        fila.subida_en = subida_en
    db.flush()
    return fila


# --------------------------------------------------------------------------
# calificacion (RF-031, RN-10)
# --------------------------------------------------------------------------
def obtener_calificacion(db: Session, reserva_id: int) -> Calificacion | None:
    """The rating of one service, or ``None``. RN-10 allows at most one."""
    consulta = select(Calificacion).where(Calificacion.reserva_id == reserva_id)
    return db.scalars(consulta).first()


def crear_calificacion(
    db: Session,
    *,
    reserva_id: int,
    usuario_id: int,
    operario_id: int | None,
    servicio_id: int,
    puntuacion: int,
    comentario: str | None,
    creada_en: datetime,
) -> Calificacion:
    fila = Calificacion(
        reserva_id=reserva_id,
        usuario_id=usuario_id,
        operario_id=operario_id,
        servicio_id=servicio_id,
        puntuacion=puntuacion,
        comentario=comentario,
        creada_en=creada_en,
    )
    db.add(fila)
    db.flush()
    return fila


def agregado_por_servicio(db: Session, servicio_id: int) -> tuple[int, int]:
    """``(conteo, suma)`` of every rating of one service (RF-031, RF-033)."""
    fila = db.execute(
        select(
            func.count(Calificacion.id), func.coalesce(func.sum(Calificacion.puntuacion), 0)
        ).where(Calificacion.servicio_id == servicio_id)
    ).one()
    return int(fila[0] or 0), int(fila[1] or 0)


def agregado_por_operario(db: Session, operario_id: int) -> tuple[int, int]:
    """``(conteo, suma)`` of every rating of one operator (RF-031, RF-033)."""
    fila = db.execute(
        select(
            func.count(Calificacion.id), func.coalesce(func.sum(Calificacion.puntuacion), 0)
        ).where(Calificacion.operario_id == operario_id)
    ).one()
    return int(fila[0] or 0), int(fila[1] or 0)
