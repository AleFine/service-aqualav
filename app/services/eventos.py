"""Domain event log helper (EXTENSION POINT P7).

Every mutation listed in the contract writes one row in ``evento_dominio``.
Nothing reads it in the MVP; it is the raw material of the v0.2 notification
feed (RF-029) and of the v1.0 reports, and back-filling it later is impossible.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.models import EventoDominio
from app.repositories import evento as evento_repo

# Entities, kept as constants so a typo cannot silently split the log in two.
ENTIDAD_USUARIO = "usuario"
ENTIDAD_VEHICULO = "vehiculo"
ENTIDAD_SERVICIO = "servicio"
ENTIDAD_RESERVA = "reserva"
ENTIDAD_PAGO = "pago"
ENTIDAD_BAHIA = "bahia"
ENTIDAD_AGENDA = "agenda"

# Actions (contract section 1, ``evento_dominio``).
USUARIO_REGISTRADO = "usuario.registrado"
#: RF-004 CA-02: the audit trail of a role change keeps author, date and the
#: value BEFORE the change, which is why ``datos`` carries both values.
USUARIO_ROL_CAMBIADO = "usuario.rol_cambiado"
#: RF-004 flow 4a. Written by the hook INC-3 turns into a real revocation.
USUARIO_TOKENS_REVOCADOS = "usuario.tokens_revocados"
#: RF-035. The temporary password is NEVER part of ``datos`` (RNF-014).
USUARIO_CREADO = "usuario.creado"
USUARIO_ACTUALIZADO = "usuario.actualizado"
USUARIO_DESACTIVADO = "usuario.desactivado"
USUARIO_ACTIVADO = "usuario.activado"
USUARIO_PASSWORD_TEMPORAL_ENVIADA = "usuario.password_temporal_enviada"
USUARIO_PASSWORD_TEMPORAL_NO_ENVIADA = "usuario.password_temporal_no_enviada"
VEHICULO_REGISTRADO = "vehiculo.registrado"
SERVICIO_CREADO = "servicio.creado"
SERVICIO_ACTUALIZADO = "servicio.actualizado"
SERVICIO_PRECIO_CAMBIADO = "servicio.precio_cambiado"
RESERVA_CREADA = "reserva.creada"
RESERVA_CANCELADA = "reserva.cancelada"
RESERVA_CHECK_IN = "reserva.check_in"
RESERVA_ESTADO_CAMBIADO = "reserva.estado_cambiado"
RESERVA_CHECK_OUT = "reserva.check_out"
#: RF-019 flow 1a: a walk-in customer served without a previous booking.
RESERVA_ATENCION_INMEDIATA = "reserva.atencion_inmediata"
#: RF-020: bay and operator assignment, and the waiting queue of flow 2a.
RESERVA_ASIGNADA = "reserva.asignada"
RESERVA_ENCOLADA = "reserva.encolada"
#: RF-024 flow 3a: the customer objected to the result.
RESERVA_EN_REVISION = "reserva.en_revision"
#: RF-024 step 4: the delivery opens the rating window. INC-6 (RF-031) reads
#: this event to decide when the seven calendar days of RN-10 start counting.
RESERVA_CALIFICACION_HABILITADA = "reserva.calificacion_habilitada"
PAGO_REGISTRADO = "pago.registrado"
#: RF-018 / bay administration.
BAHIA_CREADA = "bahia.creada"
BAHIA_ACTUALIZADA = "bahia.actualizada"
AGENDA_BLOQUEO_CREADO = "agenda.bloqueo_creado"
AGENDA_BLOQUEO_ELIMINADO = "agenda.bloqueo_eliminado"
AGENDA_DIA_NO_LABORABLE_CREADO = "agenda.dia_no_laborable_creado"
AGENDA_DIA_NO_LABORABLE_ELIMINADO = "agenda.dia_no_laborable_eliminado"
AGENDA_HORARIO_ACTUALIZADO = "agenda.horario_actualizado"


def registrar_evento(
    db: Session,
    entidad: str,
    entidad_id: int,
    accion: str,
    autor_id: int | None = None,
    datos: dict[str, Any] | None = None,
) -> EventoDominio:
    """Append one row to the event log.

    ``datos`` must be JSON serializable: callers pass ISO strings, never
    ``datetime`` instances.
    """
    return evento_repo.crear(
        db,
        entidad=entidad,
        entidad_id=entidad_id,
        accion=accion,
        autor_id=autor_id,
        datos=dict(datos or {}),
    )
