"""Domain event log helper (EXTENSION POINT P7).

Every mutation listed in the contract writes one row in ``evento_dominio``.
The MVP only ever wrote it; INC-6 is the first increment that READS it back, and
for a reason worth knowing: ``reserva.calificacion_habilitada`` is the only
record of when the rating window of RN-10 opened, and of who was working the
service at that moment. Back-filling either is impossible, which is precisely
the argument for having written the log from day one.
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
#: RF-027 / RF-028. The receipt and the reversal are audited as entities of
#: their own so RF-036 can filter by them without parsing a payment payload.
ENTIDAD_COMPROBANTE = "comprobante"
ENTIDAD_REEMBOLSO = "reembolso"
ENTIDAD_BAHIA = "bahia"
ENTIDAD_AGENDA = "agenda"
ENTIDAD_PAQUETE = "paquete"
ENTIDAD_PROMOCION = "promocion"
ENTIDAD_ADICIONAL = "servicio_adicional"
#: RF-023 and RF-031 have no entity of their own: a photograph and a rating
#: are things that happened TO A SERVICE, so they are logged under
#: ``reserva`` and land in the same timeline as its check-in and its
#: delivery, which is the timeline RF-036 is going to read.

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
#: RF-001 step 5 and flow 5a: the verification mail left, or it did not and the
#: account was created anyway so the customer can ask for another one.
USUARIO_VERIFICACION_ENVIADA = "usuario.verificacion_enviada"
USUARIO_VERIFICACION_NO_ENVIADA = "usuario.verificacion_no_enviada"
USUARIO_CORREO_VERIFICADO = "usuario.correo_verificado"
#: RF-006 flow 3a: the address was NOT changed yet, only asked to be.
USUARIO_CORREO_CAMBIO_SOLICITADO = "usuario.correo_cambio_solicitado"
USUARIO_CORREO_CAMBIADO = "usuario.correo_cambiado"
#: RF-003. The token itself is never part of ``datos`` (RNF-014).
USUARIO_RECUPERACION_SOLICITADA = "usuario.recuperacion_solicitada"
USUARIO_PASSWORD_RESTABLECIDA = "usuario.password_restablecida"
#: RF-005: one session closed by its owner, as opposed to a mass revocation.
USUARIO_SESION_CERRADA = "usuario.sesion_cerrada"
#: RF-006: the editable part of the profile.
USUARIO_PERFIL_ACTUALIZADO = "usuario.perfil_actualizado"
#: RF-029 precondition: the customer authorized push on one device. The token
#: itself is NOT part of ``datos`` (RNF-014): it is a delivery credential.
USUARIO_DISPOSITIVO_REGISTRADO = "usuario.dispositivo_registrado"
USUARIO_DISPOSITIVO_DADO_DE_BAJA = "usuario.dispositivo_dado_de_baja"
VEHICULO_REGISTRADO = "vehiculo.registrado"
#: RF-008: edition, logical deletion, and the verification RN-01 asks for.
VEHICULO_ACTUALIZADO = "vehiculo.actualizado"
VEHICULO_DADO_DE_BAJA = "vehiculo.dado_de_baja"
VEHICULO_VERIFICADO = "vehiculo.verificado"
SERVICIO_CREADO = "servicio.creado"
SERVICIO_ACTUALIZADO = "servicio.actualizado"
SERVICIO_PRECIO_CAMBIADO = "servicio.precio_cambiado"
#: RF-010 v1.0 / RNF-014: changing a vehicle factor is a tariff change, so it
#: is audited with the old and the new value, exactly like a price change.
SERVICIO_FACTOR_CAMBIADO = "servicio.factor_cambiado"
#: RF-011: packages, promotions and add-ons.
PAQUETE_CREADO = "paquete.creado"
PAQUETE_ACTUALIZADO = "paquete.actualizado"
PROMOCION_CREADA = "promocion.creada"
PROMOCION_ACTUALIZADA = "promocion.actualizada"
ADICIONAL_CREADO = "servicio_adicional.creado"
ADICIONAL_ACTUALIZADO = "servicio_adicional.actualizado"
#: RF-012: the breakdown frozen into the reservation, and its two alternate
#: flows - a coupon that was refused (3a) and a total clamped to zero (4a).
TARIFA_CALCULADA = "tarifa.calculada"
TARIFA_CUPON_RECHAZADO = "tarifa.cupon_rechazado"
TARIFA_TOTAL_LIMITADO = "tarifa.total_limitado_a_cero"
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
#: RF-024 step 4: the delivery opens the rating window. It is the ONLY record
#: of when the seven calendar days of RN-10 start counting, and it carries the
#: operator snapshot because ``asignacion_servicio`` is deleted by the very
#: move that writes this row. ``calificacion_service`` reads it back.
RESERVA_CALIFICACION_HABILITADA = "reserva.calificacion_habilitada"
#: RF-031: the customer's verdict. The COMMENT is not part of ``datos`` - the
#: event log is an audit trail, not a second copy of the review.
RESERVA_CALIFICADA = "reserva.calificada"
#: RF-023: one photograph of the service was recorded, and whether its bytes
#: made it. ``evidencia.no_subida`` is flow 4a: the row is waiting for the
#: device to retry, and this is what tells the shop it is not there yet.
EVIDENCIA_REGISTRADA = "evidencia.registrada"
EVIDENCIA_NO_SUBIDA = "evidencia.no_subida"
#: RF-030: the two-hour reminder and the three answers it admits. Flow 3a -
#: nobody answered - writes nothing on purpose: the reservation simply stays
#: confirmed, and the absence of these events IS the record of that.
RESERVA_RECORDATORIO_ENVIADO = "reserva.recordatorio_enviado"
RESERVA_ASISTENCIA_CONFIRMADA = "reserva.asistencia_confirmada"
#: RF-030 + RF-015: the customer asked to move the appointment. INC-7 turns the
#: intent into the real move; the intent is recorded now so it is not lost.
RESERVA_REPROGRAMACION_SOLICITADA = "reserva.reprogramacion_solicitada"
#: RF-020 flow 2a, closed by the scheduler: a queued vehicle got its bay
#: without the counter having to retry the assignment by hand.
RESERVA_PROMOVIDA_DE_COLA = "reserva.promovida_de_cola"
PAGO_REGISTRADO = "pago.registrado"
#: RF-026 v1.0: the online charge through the gateway, with the external
#: identifier step 4 demands. The card number is NEVER part of ``datos``
#: (RNF-013): only the token and the gateway's reference travel.
PAGO_EN_LINEA_APROBADO = "pago.en_linea_aprobado"
#: RF-026 flow 3a: the gateway said no, with its reason.
PAGO_EN_LINEA_RECHAZADO = "pago.en_linea_rechazado"
#: RF-026 postcondition: accepted but not settled yet.
PAGO_EN_LINEA_PENDIENTE = "pago.en_linea_pendiente"
#: RF-026 flow 3b: the reply never arrived and the state was asked for with the
#: SAME idempotency key instead of charging again. This event is the proof.
PAGO_EN_LINEA_CONSULTADO = "pago.en_linea_consultado"
#: RF-025 flow 3a: the gateway was down and the presential alternative was
#: offered. The reservation is untouched, which is the whole point.
PAGO_PASARELA_NO_DISPONIBLE = "pago.pasarela_no_disponible"
#: RF-025 flow 4a: the customer moved between online and counter payment.
RESERVA_MODALIDAD_PAGO_CAMBIADA = "reserva.modalidad_pago_cambiada"
#: RF-014 flow 2a: the fifteen minute window went by without payment.
RESERVA_PAGO_EXPIRADO = "reserva.pago_expirado"
#: RF-027: the receipt was numbered, rendered and stored - or it was not, and
#: flow 1a says the administrator has to find out from somewhere.
COMPROBANTE_EMITIDO = "comprobante.emitido"
COMPROBANTE_NO_EMITIDO = "comprobante.no_emitido"
#: RF-028: the reversal, and the one the gateway refused. A refused reversal is
#: NOT an error swallowed somewhere - it is a row waiting for a human.
REEMBOLSO_PROCESADO = "reembolso.procesado"
REEMBOLSO_PENDIENTE_MANUAL = "reembolso.pendiente_manual"
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
