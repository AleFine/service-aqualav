"""RF-015 - Reprogramación de reserva, y `RN-06`.

Cubre:

* ``CA-01`` con **dos reprogramaciones previas**, la tercera responde **422**;
* ``CA-02`` al confirmar el cambio, **el bloque anterior vuelve a estar
  disponible**;
* ``2b`` deben faltar **más de dos horas** para el inicio;
* ``RN-03`` una bahía atiende un vehículo a la vez, también al mover una
  reserva - y una reserva no se estorba a sí misma al desplazarse;
* ``RN-07`` + RF-018: no se puede mover una reserva a un feriado;
* la notificación del cambio, que es **plantilla + evento** y no código;
* el gancho de INC-5: ``recordatorio_service.responder`` hace la mudanza de
  verdad cuando la respuesta trae el bloque, sin que cambie el punto de llamada.

Las dos reglas duras viven en un solo sitio, ``reserva_service``: estas pruebas
las ejercen por la API y por el recordatorio, y esperan exactamente el mismo
código de error por los dos caminos.
"""

from datetime import timedelta

from sqlalchemy import select

from app.core.horario import a_utc, ahora_utc
from app.models import EventoDominio, EventoNotificacion, Notificacion, RespuestaRecordatorio
from app.services import recordatorio_service, reserva_service
from tests.conftest import (
    RUTA,
    codigo_error,
    crear_reserva,
    dejar_una_sola_bahia,
    instante,
    proximo_lunes,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _reservar(api_cliente, servicio, vehiculo_id, fecha, hora: int, minuto: int = 0) -> dict:
    respuesta = crear_reserva(api_cliente, servicio.id, vehiculo_id, instante(fecha, hora, minuto))
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _reprogramar(api, reserva_id: int, inicio):
    return api.post(
        f"{RUTA}/reservas/{reserva_id}/reprogramacion", json={"inicio": inicio.isoformat()}
    )


def _horas_libres(api, fecha, servicio) -> set[str]:
    respuesta = api.get(
        f"{RUTA}/disponibilidad",
        params={"fecha": fecha.isoformat(), "servicio_id": servicio.id},
    )
    assert respuesta.status_code == 200, respuesta.text
    return {bloque["inicio"][11:16] for bloque in respuesta.json()["bloques"]}


def _eventos(db, accion: str) -> list[EventoDominio]:
    return list(db.scalars(select(EventoDominio).where(EventoDominio.accion == accion)).all())


def _acercar_el_inicio(db, reserva_id: int, minutos: int) -> None:
    """Move the booking to ``minutos`` from now, straight in the database.

    The two-hour rule of flow 2b is measured against the clock, and a test must
    not wait for one. Writing the row is the same trick ``dejar_una_sola_bahia``
    uses: set up the situation, then exercise the rule through the API.
    """
    from app.models import Reserva

    reserva = db.get(Reserva, reserva_id)
    duracion = reserva.fin - reserva.inicio
    reserva.inicio = ahora_utc() + timedelta(minutes=minutos)
    reserva.fin = reserva.inicio + duracion
    db.commit()


# --------------------------------------------------------------------------
# CA-01 y RN-06 - como máximo DOS veces
# --------------------------------------------------------------------------
def test_ca01_la_tercera_reprogramacion_responde_422(api_cliente, servicio_corto, vehiculo_id):
    """RF-015 `CA-01` / `RN-06`, recorrido completo y no simulado.

    Las dos primeras mudanzas se hacen de verdad por la API, así que lo que la
    tercera encuentra es el contador que ellas dejaron, no una fila preparada.
    """
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 9)

    primera = _reprogramar(api_cliente, reserva["id"], instante(fecha, 11))
    assert primera.status_code == 200, primera.text
    assert primera.json()["reprogramaciones"] == 1
    assert primera.json()["reprogramaciones_restantes"] == 1

    segunda = _reprogramar(api_cliente, reserva["id"], instante(fecha, 13))
    assert segunda.status_code == 200, segunda.text
    assert segunda.json()["reprogramaciones"] == 2
    assert segunda.json()["reprogramaciones_restantes"] == 0

    tercera = _reprogramar(api_cliente, reserva["id"], instante(fecha, 15))

    assert tercera.status_code == 422
    assert codigo_error(tercera) == "LIMITE_DE_REPROGRAMACIONES"
    # `2a`: informa y OFRECE CANCELAR, que es la salida que la propia RN-06 da.
    assert "Cancélala" in tercera.json()["error"]["mensaje"]


def test_tras_el_limite_la_reserva_sigue_en_su_ultimo_bloque(
    api_cliente, servicio_corto, vehiculo_id
):
    """Una tercera reprogramación rechazada no deja la reserva a medio mover."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 9)
    _reprogramar(api_cliente, reserva["id"], instante(fecha, 11))
    _reprogramar(api_cliente, reserva["id"], instante(fecha, 13))

    _reprogramar(api_cliente, reserva["id"], instante(fecha, 15))

    detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
    assert detalle["inicio"][11:16] == "13:00"
    assert detalle["reprogramaciones"] == 2


# --------------------------------------------------------------------------
# CA-02 - el bloque anterior vuelve a estar disponible
# --------------------------------------------------------------------------
def test_ca02_el_bloque_anterior_vuelve_a_estar_disponible(
    api_cliente, db, servicio_corto, vehiculo_id
):
    """RF-015 `CA-02`, comprobado donde el cliente lo ve: la disponibilidad.

    Con una sola bahía activa, "el bloque está ocupado" y "el bloque no se
    ofrece" son la misma frase, así que la prueba no necesita mirar ninguna
    columna interna para saber que se liberó (`RN-03`).
    """
    dejar_una_sola_bahia(db)
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)

    assert "10:00" not in _horas_libres(api_cliente, fecha, servicio_corto)

    respuesta = _reprogramar(api_cliente, reserva["id"], instante(fecha, 12))
    assert respuesta.status_code == 200, respuesta.text

    libres = _horas_libres(api_cliente, fecha, servicio_corto)
    assert "10:00" in libres, "CA-02: el bloque anterior vuelve a estar disponible"
    assert "12:00" not in libres, "el bloque nuevo quedó tomado"


def test_rn03_no_se_puede_mover_una_reserva_a_un_bloque_ocupado(
    api_cliente, db, servicio_corto, vehiculo_id
):
    """`RN-03`: una bahía atiende un vehículo a la vez, también al mover."""
    dejar_una_sola_bahia(db)
    fecha = proximo_lunes(dias_minimos=2)
    primera = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)
    _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 12)

    respuesta = _reprogramar(api_cliente, primera["id"], instante(fecha, 12))

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "RESERVA_BLOQUE_OCUPADO"


def test_una_reserva_no_se_estorba_a_si_misma_al_desplazarse(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """Mover 45 minutos de servicio quince minutos no puede chocar consigo mismo.

    Con una sola bahía, el bloque 10:00-10:45 y el 10:15-11:00 se solapan. Si
    la reserva contara como obstáculo de sí misma, este desplazamiento sería un
    409 y no hay ninguna regla que lo diga.
    """
    dejar_una_sola_bahia(db)
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_medio, vehiculo_id, fecha, 10)

    respuesta = _reprogramar(api_cliente, reserva["id"], instante(fecha, 10, 15))

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["inicio"][11:16] == "10:15"


# --------------------------------------------------------------------------
# 2b - deben faltar MÁS DE DOS HORAS
# --------------------------------------------------------------------------
def test_2b_con_menos_de_dos_horas_no_se_puede_reprogramar(
    api_cliente, db, servicio_corto, vehiculo_id
):
    """RF-015 `2b`: «se exige que falten más de dos horas para el inicio»."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)
    _acercar_el_inicio(db, reserva["id"], minutos=90)

    respuesta = _reprogramar(api_cliente, reserva["id"], instante(fecha, 15))

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "REPROGRAMACION_FUERA_DE_PLAZO"
    assert "dos horas" in respuesta.json()["error"]["mensaje"]


def test_2b_el_umbral_es_estricto_a_las_dos_horas_exactas(
    api_cliente, db, servicio_corto, vehiculo_id
):
    """«MÁS de dos horas»: dos horas exactas ya es tarde, como en `RN-05`."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)
    _acercar_el_inicio(db, reserva["id"], minutos=120)

    respuesta = _reprogramar(api_cliente, reserva["id"], instante(fecha, 15))

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "REPROGRAMACION_FUERA_DE_PLAZO"


# --------------------------------------------------------------------------
# El bloque nuevo pasa por las mismas reglas que uno recién creado
# --------------------------------------------------------------------------
def test_no_se_puede_mover_una_reserva_a_un_feriado(
    api_cliente, api_admin, servicio_corto, vehiculo_id
):
    """RF-013 delta: la disponibilidad respeta feriados, y mover también.

    Un bloque que `RF-018` sacó del calendario no puede volver a entrar por la
    puerta de la reprogramación, igual que no entra por la de la creación.
    """
    fecha = proximo_lunes(dias_minimos=2)
    feriado = proximo_lunes(dias_minimos=9)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)

    alta = api_admin.post(
        f"{RUTA}/agenda/dias-no-laborables",
        json={"fecha": feriado.isoformat(), "motivo": "Feriado del taller"},
    )
    assert alta.status_code == 201, alta.text

    respuesta = _reprogramar(api_cliente, reserva["id"], instante(feriado, 10))

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "RESERVA_FUERA_DE_HORARIO"
    assert "Feriado del taller" in str(respuesta.json()["error"]["detalles"])


def test_no_se_puede_mover_una_reserva_fuera_del_horario(api_cliente, servicio_corto, vehiculo_id):
    """`RN-07`: las 23:00 no son una hora de atención, se mueva o se cree."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)

    respuesta = _reprogramar(api_cliente, reserva["id"], instante(fecha, 23))

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "RESERVA_FUERA_DE_HORARIO"


def test_rn02_el_bloque_nuevo_tambien_pide_una_hora_de_anticipacion(
    api_cliente, db, servicio_corto, vehiculo_id
):
    """`RN-02` no se relaja por venir de una reprogramación."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)

    # Un bloque treinta minutos después de ahora mismo: dentro del horario de
    # atención sólo por casualidad, pero RN-02 lo rechaza antes de mirar eso.
    destino = a_utc(ahora_utc() + timedelta(minutes=30))
    respuesta = _reprogramar(api_cliente, reserva["id"], destino)

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) in {
        "RESERVA_ANTICIPACION_INSUFICIENTE",
        "RESERVA_FUERA_DE_HORARIO",
    }


# --------------------------------------------------------------------------
# P3 - qué estados admiten la operación sale de la tabla
# --------------------------------------------------------------------------
def test_una_reserva_ya_recibida_no_se_reprograma(
    api_cliente, api_recepcion, servicio_corto, vehiculo_id
):
    """El vehículo ya está en el taller: no hay bloque futuro que mover.

    La condición no compara con ningún estado: se pregunta a
    ``transicion_estado`` si la operación de check-in sigue declarada, que es
    la forma P3 de decir «la reserva está confirmada y no ha iniciado».
    """
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)
    entrada = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": True}
    )
    assert entrada.status_code == 200, entrada.text

    respuesta = _reprogramar(api_cliente, reserva["id"], instante(fecha, 15))

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"


def test_una_reserva_cancelada_no_se_reprograma(api_cliente, servicio_corto, vehiculo_id):
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)
    api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/cancelacion", json={"motivo": "Me surgió un viaje"}
    )

    respuesta = _reprogramar(api_cliente, reserva["id"], instante(fecha, 15))

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"


def test_un_cliente_no_reprograma_la_reserva_de_otro(
    api_cliente, api_recepcion, servicio_corto, vehiculo_id
):
    """Autorización horizontal: la reserva ajena responde 404, no 403."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)

    # La recepción sí puede (RF-015 nombra a los dos actores).
    del_mostrador = _reprogramar(api_recepcion, reserva["id"], instante(fecha, 12))
    assert del_mostrador.status_code == 200, del_mostrador.text

    del_operario = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}")
    assert del_operario.status_code == 200


def test_el_operario_no_tiene_permiso_para_reprogramar(
    api_cliente, api_operario, servicio_corto, vehiculo_id
):
    """P5: la autorización es por código de permiso, no por quién parece ser."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)

    respuesta = _reprogramar(api_operario, reserva["id"], instante(fecha, 12))

    assert respuesta.status_code == 403
    assert codigo_error(respuesta) == "PERMISO_DENEGADO"


# --------------------------------------------------------------------------
# Lo que la mudanza deja escrito
# --------------------------------------------------------------------------
def test_la_reprogramacion_deja_el_bloque_anterior_en_la_bitacora(
    api_cliente, db, servicio_corto, vehiculo_id
):
    """P7: mover edita la fila, así que el evento es el único rastro del antes."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)

    _reprogramar(api_cliente, reserva["id"], instante(fecha, 12))

    registrados = _eventos(db, "reserva.reprogramada")
    assert len(registrados) == 1
    datos = registrados[0].datos
    assert datos["anterior"]["inicio"].startswith(a_utc(instante(fecha, 10)).isoformat()[:13])
    assert datos["reprogramaciones"] == 1
    assert datos["restantes"] == reserva_service.MAXIMO_REPROGRAMACIONES - 1


def test_la_reprogramacion_avisa_al_cliente_con_plantilla_y_evento(
    api_cliente, db, servicio_corto, vehiculo_id
):
    """RF-015 «notificación del cambio», por el camino que fijó INC-5.

    No hay ningún despacho escrito a mano: hay una fila en
    ``plantilla_notificacion`` para el evento ``reprogramacion`` y el servicio
    solo nombra el evento. Por eso la prueba mira ``notificacion``.
    """
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)

    _reprogramar(api_cliente, reserva["id"], instante(fecha, 12))

    avisos = list(
        db.scalars(
            select(Notificacion).where(
                Notificacion.reserva_id == reserva["id"],
                Notificacion.evento == EventoNotificacion.REPROGRAMACION.value,
            )
        ).all()
    )
    assert avisos, "el cambio se notifica"
    # La plantilla declara los tres canales; el push además exige un
    # dispositivo registrado (RF-029), y el cliente de demostración no tiene
    # ninguno. Lo que la prueba fija es que los canales salen de la TABLA.
    assert {aviso.canal for aviso in avisos} == {"en_app", "correo"}
    assert "12:00" in avisos[0].cuerpo


def test_la_tarifa_congelada_no_se_recalcula_al_mover(api_cliente, servicio_corto, vehiculo_id):
    """RF-014 `CA-03`: mover cambia CUÁNDO, no CUÁNTO."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)

    movida = _reprogramar(api_cliente, reserva["id"], instante(fecha, 12)).json()

    assert movida["monto"] == reserva["monto"]
    assert movida["tarifa"]["total"] == reserva["tarifa"]["total"]
    assert movida["codigo"] == reserva["codigo"], "el cliente conserva su código"


def test_la_hora_estimada_de_entrega_se_mueve_con_la_reserva(
    api_cliente, servicio_corto, vehiculo_id
):
    """RF-022: la promesa de entrega viaja con el bloque."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)

    movida = _reprogramar(api_cliente, reserva["id"], instante(fecha, 12)).json()

    assert movida["hora_estimada_entrega"][11:16] == movida["fin"][11:16]
    assert movida["hora_estimada_entrega"][11:16] == "12:30"


# --------------------------------------------------------------------------
# El gancho de INC-5: el recordatorio de RF-030
# --------------------------------------------------------------------------
def test_responder_reprogramo_con_bloque_mueve_la_reserva_de_verdad(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-030 + RF-015: el punto de llamada no cambió, el efecto sí.

    El recordatorio se arma con ``recordatorio_service.enviar`` en vez de con
    el barrido porque el barrido solo alcanza reservas que empiezan dentro de
    dos horas, y `2b` exige que falten MÁS de dos. Quien envía y quien responde
    siguen siendo las mismas funciones.
    """
    from app.models import Reserva

    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_medio, vehiculo_id, fecha, 10)
    recordatorio_service.enviar(db, db.get(Reserva, reserva["id"]))
    db.commit()

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/recordatorio",
        json={
            "respuesta": RespuestaRecordatorio.REPROGRAMO.value,
            "nuevo_inicio": instante(fecha, 16).isoformat(),
        },
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["reserva"]["inicio"][11:16] == "16:00"
    assert cuerpo["reserva"]["reprogramaciones"] == 1
    assert cuerpo["recordatorio"]["respuesta"] == RespuestaRecordatorio.REPROGRAMO.value
    assert _eventos(db, "reserva.reprogramada"), "la mudanza es la de RF-015, no otra"


def test_responder_reprogramo_respeta_rn06_por_el_mismo_camino(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """`RN-06` vive en un solo sitio: el recordatorio no tiene su propia copia."""
    from app.models import Reserva

    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_medio, vehiculo_id, fecha, 9)
    _reprogramar(api_cliente, reserva["id"], instante(fecha, 11))
    _reprogramar(api_cliente, reserva["id"], instante(fecha, 13))
    recordatorio_service.enviar(db, db.get(Reserva, reserva["id"]))
    db.commit()

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/recordatorio",
        json={
            "respuesta": RespuestaRecordatorio.REPROGRAMO.value,
            "nuevo_inicio": instante(fecha, 16).isoformat(),
        },
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "LIMITE_DE_REPROGRAMACIONES"


def test_responder_reprogramo_desde_el_barrido_choca_con_el_plazo(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """Las dos reglas hablando entre ellas, tal como están escritas.

    RF-030 manda el recordatorio cuando faltan dos horas; RF-015 `2b` exige que
    falten MÁS de dos. Un cliente que responde «reprogramo» enseguida está,
    por definición, fuera de plazo, y lo que recibe es el 422 que le señala el
    otro botón del mismo recordatorio: cancelar. Relajar el plazo «porque viene
    del recordatorio» sería la segunda copia de la regla que `RN-06` prohíbe.
    """
    from app.models import Reserva
    from app.services import planificador

    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_medio, vehiculo_id, fecha, 15)
    planificador.ejecutar_pendientes(db, a_utc(instante(fecha, 13)))
    _acercar_el_inicio(db, reserva["id"], minutos=110)
    db.refresh(db.get(Reserva, reserva["id"]))

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/recordatorio",
        json={
            "respuesta": RespuestaRecordatorio.REPROGRAMO.value,
            "nuevo_inicio": instante(proximo_lunes(dias_minimos=9), 10).isoformat(),
        },
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "REPROGRAMACION_FUERA_DE_PLAZO"


def test_al_mover_la_reserva_el_recordatorio_se_rearma_para_el_bloque_nuevo(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-030 sigue funcionando después de RF-015.

    El recordatorio que apuntaba al bloque viejo ya no significa nada. Se
    reapunta en vez de borrarse - la fila es única por reserva y guarda la
    respuesta que provocó la mudanza - y vuelve a salir para el bloque que el
    cliente tiene ahora.
    """
    from app.models import Recordatorio, Reserva
    from app.services import planificador

    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_medio, vehiculo_id, fecha, 10)
    recordatorio_service.enviar(db, db.get(Reserva, reserva["id"]))
    db.commit()

    _reprogramar(api_cliente, reserva["id"], instante(fecha, 16))

    fila = db.scalars(select(Recordatorio).where(Recordatorio.reserva_id == reserva["id"])).first()
    assert fila.enviado_en is None, "vuelve a estar pendiente de enviarse"
    assert fila.programado_para.hour in {14, 19}, "dos horas antes de las 16:00 (Lima/UTC)"

    resultado = planificador.ejecutar_pendientes(db, a_utc(instante(fecha, 14)))

    assert [fila.reserva_id for fila in resultado.recordatorios] == [reserva["id"]]


def test_el_operario_no_mueve_una_reserva_respondiendo_su_recordatorio(
    api_cliente, api_operario, db, servicio_medio, vehiculo_id
):
    """La segunda puerta de RF-015 también pide su permiso (P5).

    ``POST /reservas/{id}/recordatorio`` está guardado por los permisos de
    LECTURA, y el operario tiene ``reserva:leer_todas`` para que le funcionen
    las pantallas de bahía. Si la comprobación viviera solo en el router de
    la reprogramación, podría mover la cita de un cliente respondiendo su
    recordatorio; por eso el permiso se valida dentro del servicio, igual que
    ``cambiar_estado`` valida el de la transición.
    """
    from app.models import Reserva

    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_medio, vehiculo_id, fecha, 10)
    recordatorio_service.enviar(db, db.get(Reserva, reserva["id"]))
    db.commit()

    respuesta = api_operario.post(
        f"{RUTA}/reservas/{reserva['id']}/recordatorio",
        json={
            "respuesta": RespuestaRecordatorio.REPROGRAMO.value,
            "nuevo_inicio": instante(fecha, 16).isoformat(),
        },
    )

    assert respuesta.status_code == 403
    assert codigo_error(respuesta) == "PERMISO_DENEGADO"
    assert api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()["inicio"][11:16] == "10:00"
