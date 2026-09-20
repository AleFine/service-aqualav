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

# Actions (contract section 1, ``evento_dominio``).
USUARIO_REGISTRADO = "usuario.registrado"
#: RF-004 CA-02: the audit trail of a role change keeps author, date and the
#: value BEFORE the change, which is why ``datos`` carries both values.
USUARIO_ROL_CAMBIADO = "usuario.rol_cambiado"
#: RF-004 flow 4a. Written by the hook INC-3 turns into a real revocation.
USUARIO_TOKENS_REVOCADOS = "usuario.tokens_revocados"
VEHICULO_REGISTRADO = "vehiculo.registrado"
SERVICIO_CREADO = "servicio.creado"
SERVICIO_ACTUALIZADO = "servicio.actualizado"
SERVICIO_PRECIO_CAMBIADO = "servicio.precio_cambiado"
RESERVA_CREADA = "reserva.creada"
RESERVA_CANCELADA = "reserva.cancelada"
RESERVA_CHECK_IN = "reserva.check_in"
RESERVA_ESTADO_CAMBIADO = "reserva.estado_cambiado"
RESERVA_CHECK_OUT = "reserva.check_out"
PAGO_REGISTRADO = "pago.registrado"


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
