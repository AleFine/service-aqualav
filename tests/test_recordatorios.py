"""RF-030 - Recordatorio automático de reserva, y el planificador simulado.

Cubre:

* ``CA-01`` una reserva de las 15:00 ya tiene su recordatorio a las 13:00;
* ``CA-02`` una reserva cancelada no recibe recordatorio;
* ``3a`` sin respuesta, la reserva sigue confirmada;
* las tres acciones del recordatorio: confirmar asistencia, reprogramar y
  cancelar (con su flujo de cancelación y su política de penalidad);
* la oportunidad que dejó INC-1B: el planificador promueve la cola de espera
  de RF-020 cuando se libera una bahía.

El planificador es una función pura de ``(db, momento)``: ninguna prueba espera
a que llegue una hora, le dice qué hora es.
"""

from datetime import timedelta

from sqlalchemy import select

from app.core.horario import a_utc
from app.models import (
    EstadoRecordatorio,
    EventoNotificacion,
    Notificacion,
    Recordatorio,
    RespuestaRecordatorio,
)
from app.services import planificador, recordatorio_service
from tests.conftest import (
    RUTA,
    asignar,
    avanzar_estado,
    codigo_error,
    crear_reserva,
    dejar_una_sola_bahia,
    instante,
    proximo_lunes,
)

HORA_RESERVA = 15
HORA_BARRIDO = 13


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _reserva_a_las(api_cliente, servicio, vehiculo_id, hora: int = HORA_RESERVA) -> dict:
    inicio = instante(proximo_lunes(), hora)
    respuesta = crear_reserva(api_cliente, servicio.id, vehiculo_id, inicio)
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _barrer(db, hora: int = HORA_BARRIDO):
    """Run the sweep pretending it is ``hora`` of the booked Monday."""
    return planificador.ejecutar_pendientes(db, a_utc(instante(proximo_lunes(), hora)))


def _recordatorio(db, reserva_id: int) -> Recordatorio | None:
    return db.scalars(select(Recordatorio).where(Recordatorio.reserva_id == reserva_id)).first()


# --------------------------------------------------------------------------
# CA-01 y CA-02
# --------------------------------------------------------------------------
def test_una_reserva_de_las_15_recibe_su_recordatorio_a_las_13(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-030 CA-01, con las horas del requisito."""
    reserva = _reserva_a_las(api_cliente, servicio_medio, vehiculo_id)

    resultado = _barrer(db)

    assert len(resultado.recordatorios) == 1
    fila = _recordatorio(db, reserva["id"])
    assert fila is not None
    assert fila.estado == EstadoRecordatorio.ENVIADO.value
    assert fila.enviado_en is not None
    assert fila.respuesta is None


def test_una_reserva_cancelada_no_recibe_recordatorio(api_cliente, db, servicio_medio, vehiculo_id):
    """RF-030 CA-02.

    No hace falta nombrar el estado: una reserva cancelada no declara ninguna
    salida, así que la operación de ingreso ya no aplica y el barrido la
    descarta por construcción (P3).
    """
    reserva = _reserva_a_las(api_cliente, servicio_medio, vehiculo_id)
    cancelada = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/cancelacion", json={"motivo": "Ya no puedo ir"}
    )
    assert cancelada.status_code == 200, cancelada.text

    resultado = _barrer(db)

    assert resultado.recordatorios == []
    assert _recordatorio(db, reserva["id"]) is None


def test_una_reserva_fuera_de_la_ventana_de_dos_horas_no_se_recuerda(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """«Barrido de reservas que inician EN DOS HORAS», ni antes ni después."""
    _reserva_a_las(api_cliente, servicio_medio, vehiculo_id)

    assert _barrer(db, HORA_BARRIDO - 3).recordatorios == []


def test_el_barrido_no_envia_dos_veces_el_mismo_recordatorio(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """Una fila por reserva: ejecutar el planificador dos veces es inocuo."""
    _reserva_a_las(api_cliente, servicio_medio, vehiculo_id)

    primera = _barrer(db)
    segunda = _barrer(db)

    assert len(primera.recordatorios) == 1
    assert segunda.recordatorios == []


def test_una_reserva_que_ya_llego_al_mostrador_no_se_recuerda(
    api_cliente, api_recepcion, db, servicio_medio, vehiculo_id
):
    """El cliente ya está aquí: recordarle que venga no tiene sentido."""
    reserva = _reserva_a_las(api_cliente, servicio_medio, vehiculo_id)
    entrada = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": False}
    )
    assert entrada.status_code == 200, entrada.text

    assert _barrer(db).recordatorios == []


# --------------------------------------------------------------------------
# El contenido del recordatorio y sus tres acciones
# --------------------------------------------------------------------------
def test_el_recordatorio_ofrece_confirmar_reprogramar_o_cancelar(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-030: «recordatorio con acciones (confirmar, reprogramar o cancelar)»."""
    reserva = _reserva_a_las(api_cliente, servicio_medio, vehiculo_id)
    _barrer(db)

    filas = db.scalars(
        select(Notificacion).where(
            Notificacion.reserva_id == reserva["id"],
            Notificacion.evento == EventoNotificacion.RECORDATORIO.value,
        )
    ).all()
    assert filas

    cuerpo = filas[0].cuerpo
    for accion in RespuestaRecordatorio:
        assert accion.value in cuerpo


def test_sin_respuesta_la_reserva_sigue_confirmada(api_cliente, db, servicio_medio, vehiculo_id):
    """RF-030 flujo 3a."""
    reserva = _reserva_a_las(api_cliente, servicio_medio, vehiculo_id)
    estado_antes = reserva["estado"]

    _barrer(db)

    despues = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
    assert despues["estado"] == estado_antes
    assert _recordatorio(db, reserva["id"]).respuesta is None


def test_confirmar_la_asistencia_no_cambia_el_estado(api_cliente, db, servicio_medio, vehiculo_id):
    """Confirmar asistencia es información para el mostrador, no una transición."""
    reserva = _reserva_a_las(api_cliente, servicio_medio, vehiculo_id)
    _barrer(db)

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/recordatorio",
        json={"respuesta": RespuestaRecordatorio.CONFIRMO.value},
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["reserva"]["estado"] == reserva["estado"]
    assert cuerpo["recordatorio"]["respuesta"] == RespuestaRecordatorio.CONFIRMO.value
    assert cuerpo["recordatorio"]["estado"] == EstadoRecordatorio.RESPONDIDO.value


def test_cancelar_desde_el_recordatorio_ejecuta_la_cancelacion(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-030 flujo 4a: cancelar corre el flujo de RF-016 con su política."""
    reserva = _reserva_a_las(api_cliente, servicio_medio, vehiculo_id)
    _barrer(db)

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/recordatorio",
        json={"respuesta": RespuestaRecordatorio.CANCELO.value, "motivo": "Se me cruzó una cita"},
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["reserva"]["cancelacion"]["motivo"] == "Se me cruzó una cita"
    assert cuerpo["recordatorio"]["respuesta"] == RespuestaRecordatorio.CANCELO.value
    assert cuerpo["reserva"]["transiciones_permitidas"] == []


def test_cancelar_sin_motivo_deja_uno_en_el_expediente(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-016 CA-03 sigue exigiendo un motivo en el registro."""
    reserva = _reserva_a_las(api_cliente, servicio_medio, vehiculo_id)
    _barrer(db)

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/recordatorio",
        json={"respuesta": RespuestaRecordatorio.CANCELO.value},
    )

    assert respuesta.status_code == 200, respuesta.text
    assert (
        respuesta.json()["reserva"]["cancelacion"]["motivo"]
        == recordatorio_service.MOTIVO_POR_DEFECTO
    )


def test_reprogramar_queda_registrado_como_intencion(api_cliente, db, servicio_medio, vehiculo_id):
    """RF-030 + RF-015: la reprogramación real es INC-7.

    El camino queda abierto: la respuesta, su momento y el evento de dominio se
    guardan ahora - reconstruirlos después sería imposible - y la reserva no se
    mueve, porque RN-06 (máximo dos veces, con más de dos horas) se implementa
    en un solo sitio y ese sitio es INC-7.
    """
    reserva = _reserva_a_las(api_cliente, servicio_medio, vehiculo_id)
    _barrer(db)

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/recordatorio",
        json={"respuesta": RespuestaRecordatorio.REPROGRAMO.value, "motivo": "Prefiero el jueves"},
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["recordatorio"]["respuesta"] == RespuestaRecordatorio.REPROGRAMO.value
    assert cuerpo["reserva"]["estado"] == reserva["estado"]


def test_responder_un_recordatorio_que_no_se_envio_da_404(api_cliente, servicio_medio, vehiculo_id):
    reserva = _reserva_a_las(api_cliente, servicio_medio, vehiculo_id)

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/recordatorio",
        json={"respuesta": RespuestaRecordatorio.CONFIRMO.value},
    )

    assert respuesta.status_code == 404
    assert codigo_error(respuesta) == "RECURSO_NO_ENCONTRADO"


def test_un_cliente_no_responde_el_recordatorio_de_otro(
    api_cliente, api_operario, db, servicio_medio, vehiculo_id
):
    """Autorización horizontal: la reserva ajena responde 404, no 403."""
    reserva = _reserva_a_las(api_cliente, servicio_medio, vehiculo_id)
    _barrer(db)

    otro = api_operario.post(
        f"{RUTA}/reservas/{reserva['id']}/recordatorio",
        json={"respuesta": RespuestaRecordatorio.CANCELO.value},
    )

    # El operario sí ve todas las reservas, pero no puede cancelar.
    assert otro.status_code == 403
    assert codigo_error(otro) == "PERMISO_DENEGADO"


# --------------------------------------------------------------------------
# El endpoint interno del planificador
# --------------------------------------------------------------------------
def test_el_endpoint_interno_ejecuta_el_barrido(
    api_cliente, api_admin, db, servicio_medio, vehiculo_id
):
    """Sección 4 del plan: la misma función, invocable desde un endpoint."""
    inicio = instante(proximo_lunes(), HORA_RESERVA)
    reserva = crear_reserva(api_cliente, servicio_medio.id, vehiculo_id, inicio)
    assert reserva.status_code == 201, reserva.text

    respuesta = api_admin.post(f"{RUTA}/interno/planificador")

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["recordatorios_enviados"] == 0, "la reserva es de dentro de varios días"
    assert cuerpo["reservas_promovidas"] == []


def test_el_endpoint_interno_exige_su_permiso(api_cliente):
    """Principio P5: quién puede forzar el barrido es un permiso."""
    respuesta = api_cliente.post(f"{RUTA}/interno/planificador")

    assert respuesta.status_code == 403
    assert codigo_error(respuesta) == "PERMISO_DENEGADO"


def test_el_endpoint_interno_exige_token(cliente_http):
    assert cliente_http.post(f"{RUTA}/interno/planificador").status_code == 401


# --------------------------------------------------------------------------
# La cola de espera que INC-1B dejó sin promover (RF-020 flujo 2a)
# --------------------------------------------------------------------------
def test_el_planificador_promueve_la_cola_cuando_se_libera_una_bahia(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """INC-1B dejó la cola sin promoverse sola; el planificador la promueve.

    La promoción corre con los permisos de quien encoló el vehículo, leídos del
    evento ``reserva.encolada``: el planificador no inventa una cuenta de
    sistema con derechos que nadie concedió.
    """
    dejar_una_sola_bahia(db)

    primera = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10)
    ).json()
    segunda = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 12)
    ).json()

    for reserva in (primera, segunda):
        entrada = api_recepcion.post(
            f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": False}
        )
        assert entrada.status_code == 200, entrada.text

    assert asignar(api_recepcion, primera["id"]).json()["asignacion"] is not None
    en_cola = asignar(api_recepcion, segunda["id"]).json()
    assert en_cola["asignacion"] is None
    assert en_cola["cola"]["posicion"] == 1

    # La primera termina y entrega: la bahía queda libre.
    for estado in ("en_lavado", "secado", "acabado", "finalizado"):
        assert avanzar_estado(api_operario, primera["id"], estado).status_code == 200
    api_recepcion.post(
        f"{RUTA}/reservas/{primera['id']}/pagos",
        json={"medio": "efectivo", "monto_centimos": primera["monto"]["monto_centimos"]},
        headers={"Idempotency-Key": "pago-cola-1"},
    )
    salida = api_recepcion.post(
        f"{RUTA}/reservas/{primera['id']}/check-out", json={"conformidad_cliente": True}
    )
    assert salida.status_code == 200, salida.text

    resultado = planificador.ejecutar_pendientes(db)

    assert resultado.promovidas == [segunda["id"]]
    promovida = api_recepcion.get(f"{RUTA}/reservas/{segunda['id']}").json()
    assert promovida["estado"] == "asignado"


def test_sin_bahia_libre_la_cola_se_queda_donde_esta(
    api_cliente, api_recepcion, db, servicio_medio, vehiculo_id
):
    """El planificador no promueve lo que no cabe: el vehículo sigue esperando."""
    dejar_una_sola_bahia(db)

    primera = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10)
    ).json()
    segunda = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 12)
    ).json()
    for reserva in (primera, segunda):
        api_recepcion.post(
            f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": False}
        )
    asignar(api_recepcion, primera["id"])
    asignar(api_recepcion, segunda["id"])

    resultado = planificador.ejecutar_pendientes(db)

    assert resultado.promovidas == []
    assert api_recepcion.get(f"{RUTA}/reservas/{segunda['id']}").json()["estado"] == "en_recepcion"


# --------------------------------------------------------------------------
# El bucle de fondo
# --------------------------------------------------------------------------
def test_el_bucle_de_fondo_esta_apagado_en_las_pruebas():
    """Sección 4 del plan: desactivable por configuración, y apagado aquí.

    Ninguna prueba depende del reloj real, así que el bucle no arranca. Que el
    barrido siga siendo alcanzable - por el endpoint interno y por la función
    pura - es lo que hace que apagarlo no sea un modo degradado.
    """
    assert planificador.iniciar_bucle() is None


def test_la_ventana_del_recordatorio_es_de_dos_horas():
    """RF-030: la ventana es del requisito, no un número suelto en el código."""
    assert recordatorio_service.VENTANA_RECORDATORIO == timedelta(hours=2)
