"""Data access for the notification tables (RF-029, RF-030).

Queries only, no decisions: whether a channel may be used, how many retries are
left and what a missing template means are all resolved one layer up, in
``notificacion_service``.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Dispositivo, Notificacion, PlantillaNotificacion, Recordatorio

#: How many notices the in-app feed hands back by default.
LIMITE_BANDEJA = 50


# --------------------------------------------------------------------------
# plantilla_notificacion
# --------------------------------------------------------------------------
def obtener_plantilla(
    db: Session, evento: str, canal: str, idioma: str
) -> PlantillaNotificacion | None:
    """The text of one event on one channel in one language, or ``None``."""
    consulta = select(PlantillaNotificacion).where(
        PlantillaNotificacion.evento == evento,
        PlantillaNotificacion.canal == canal,
        PlantillaNotificacion.idioma == idioma,
    )
    return db.scalars(consulta).first()


# --------------------------------------------------------------------------
# notificacion
# --------------------------------------------------------------------------
def crear(
    db: Session,
    *,
    usuario_id: int,
    reserva_id: int | None,
    evento: str,
    canal: str,
    destino: str,
    asunto: str,
    cuerpo: str,
    estado_envio: str,
    creada_en: datetime,
) -> Notificacion:
    fila = Notificacion(
        usuario_id=usuario_id,
        reserva_id=reserva_id,
        evento=evento,
        canal=canal,
        destino=destino,
        asunto=asunto,
        cuerpo=cuerpo,
        estado_envio=estado_envio,
        intentos=0,
        creada_en=creada_en,
    )
    db.add(fila)
    db.flush()
    return fila


def anotar_intento(
    db: Session,
    fila: Notificacion,
    *,
    estado_envio: str,
    momento: datetime,
    error: str | None = None,
) -> Notificacion:
    """Record one delivery attempt on an existing row.

    ``intentos`` grows by one on every call - including the failed ones, which
    is what RF-029 CA-02 needs in order to say the retries were exhausted.
    """
    fila.intentos += 1
    fila.estado_envio = estado_envio
    fila.error = error
    if error is None:
        fila.enviado_en = momento
    db.flush()
    return fila


def cerrar(
    db: Session,
    fila: Notificacion,
    *,
    estado_envio: str,
    momento: datetime,
    error: str | None = None,
) -> Notificacion:
    """Stamp the outcome without touching ``intentos``.

    Defensive: a provider that was handed no session leaves the row untouched,
    and the dispatcher still has to say how the delivery ended.
    """
    fila.estado_envio = estado_envio
    fila.error = error
    if error is None and fila.enviado_en is None:
        fila.enviado_en = momento
    db.flush()
    return fila


def listar_por_usuario(
    db: Session, usuario_id: int, *, limite: int = LIMITE_BANDEJA
) -> list[Notificacion]:
    """The in-app feed: newest first (RF-022 reads it while polling)."""
    consulta = (
        select(Notificacion)
        .where(Notificacion.usuario_id == usuario_id)
        .order_by(Notificacion.creada_en.desc(), Notificacion.id.desc())
        .limit(limite)
    )
    return list(db.scalars(consulta).all())


def listar_por_reserva(db: Session, reserva_id: int) -> list[Notificacion]:
    consulta = (
        select(Notificacion).where(Notificacion.reserva_id == reserva_id).order_by(Notificacion.id)
    )
    return list(db.scalars(consulta).all())


# --------------------------------------------------------------------------
# dispositivo
# --------------------------------------------------------------------------
def listar_dispositivos(db: Session, usuario_id: int, *, solo_activos: bool = True):
    consulta = select(Dispositivo).where(Dispositivo.usuario_id == usuario_id)
    if solo_activos:
        consulta = consulta.where(Dispositivo.activo.is_(True))
    return list(db.scalars(consulta.order_by(Dispositivo.id)).all())


def obtener_dispositivo_por_token(db: Session, token_push: str) -> Dispositivo | None:
    consulta = select(Dispositivo).where(Dispositivo.token_push == token_push)
    return db.scalars(consulta).first()


def obtener_dispositivo(db: Session, dispositivo_id: int) -> Dispositivo | None:
    return db.get(Dispositivo, dispositivo_id)


def guardar_dispositivo(
    db: Session,
    *,
    usuario_id: int,
    token_push: str,
    plataforma: str,
    registrado_en: datetime,
) -> Dispositivo:
    """Register a token, or revive the row that already holds it."""
    fila = obtener_dispositivo_por_token(db, token_push)
    if fila is None:
        fila = Dispositivo(
            usuario_id=usuario_id,
            token_push=token_push,
            plataforma=plataforma,
            activo=True,
            registrado_en=registrado_en,
        )
        db.add(fila)
    else:
        fila.usuario_id = usuario_id
        fila.plataforma = plataforma
        fila.activo = True
        fila.registrado_en = registrado_en
    db.flush()
    return fila


def desactivar_dispositivo(db: Session, fila: Dispositivo) -> Dispositivo:
    fila.activo = False
    db.flush()
    return fila


# --------------------------------------------------------------------------
# recordatorio
# --------------------------------------------------------------------------
def obtener_recordatorio(db: Session, reserva_id: int) -> Recordatorio | None:
    consulta = select(Recordatorio).where(Recordatorio.reserva_id == reserva_id)
    return db.scalars(consulta).first()


def crear_recordatorio(
    db: Session,
    *,
    reserva_id: int,
    programado_para: datetime,
    enviado_en: datetime | None,
    estado: str,
) -> Recordatorio:
    fila = Recordatorio(
        reserva_id=reserva_id,
        programado_para=programado_para,
        enviado_en=enviado_en,
        estado=estado,
    )
    db.add(fila)
    db.flush()
    return fila


def responder_recordatorio(
    db: Session,
    fila: Recordatorio,
    *,
    respuesta: str,
    estado: str,
    momento: datetime,
) -> Recordatorio:
    fila.respuesta = respuesta
    fila.estado = estado
    fila.respondido_en = momento
    db.flush()
    return fila
