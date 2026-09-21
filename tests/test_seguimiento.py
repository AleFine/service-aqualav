"""RF-022 (delta v1.0) - Seguimiento del servicio en tiempo real.

Lo que el MVP no podía responder y v1.0 exige:

* ``porcentaje_avance`` derivado de la posición del estado en el ciclo - y
  derivado de ``transicion_estado``, no de una lista escrita a mano, que es lo
  que vigila el test-guarda AST de ``tests/test_operacion.py``;
* ``hora_estimada_entrega`` recalculada en cada cambio de estado;
* flujo ``4a``: si el retraso supera los quince minutos, se avisa al cliente
  con el nuevo horario.
"""

from datetime import timedelta

from sqlalchemy import select

from app.core.horario import ahora_utc, desde_bd
from app.models import EventoNotificacion, Notificacion, Reserva, TransicionEstado
from app.services import estado_service, seguimiento_service
from tests.conftest import (
    RUTA,
    asignar,
    avanzar_estado,
    crear_reserva,
    instante,
    llevar_hasta_finalizado,
    proximo_lunes,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _reserva(api_cliente, servicio, vehiculo_id, hora: int = 10) -> dict:
    inicio = instante(proximo_lunes(), hora)
    respuesta = crear_reserva(api_cliente, servicio.id, vehiculo_id, inicio)
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _avisos_de_retraso(db, reserva_id: int) -> list[Notificacion]:
    return list(
        db.scalars(
            select(Notificacion).where(
                Notificacion.reserva_id == reserva_id,
                Notificacion.evento == EventoNotificacion.RETRASO.value,
            )
        ).all()
    )


# --------------------------------------------------------------------------
# El porcentaje de avance sale de la tabla
# --------------------------------------------------------------------------
def test_el_avance_sale_de_la_posicion_del_estado_en_la_cadena(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-022 delta: porcentaje de avance, derivado del ciclo declarado.

    Los valores esperados se calculan aquí con la MISMA cadena que deriva
    ``estado_service``: la prueba comprueba que el avance sigue la tabla, no
    que coincide con unos números que alguien escribió a mano.
    """
    cadena = estado_service.cadena_principal(db)
    esperado = {estado: round(100 * i / (len(cadena) - 1)) for i, estado in enumerate(cadena)}

    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id)
    detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
    assert detalle["porcentaje_avance"] == esperado[detalle["estado"]]

    api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": False}
    )
    asignar(api_recepcion, reserva["id"])

    for estado in ("en_lavado", "secado", "acabado", "finalizado"):
        assert avanzar_estado(api_operario, reserva["id"], estado).status_code == 200
        detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
        assert detalle["porcentaje_avance"] == esperado[estado]

    # Y el avance crece: el cliente ve una barra que sube, no un número suelto.
    assert esperado["finalizado"] > esperado["en_lavado"] > esperado["en_recepcion"]


def test_la_reserva_entregada_reporta_el_cien_por_ciento(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id)
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])
    api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/pagos",
        json={"medio": "efectivo", "monto_centimos": reserva["monto"]["monto_centimos"]},
        headers={"Idempotency-Key": "pago-seguimiento-1"},
    )
    salida = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )
    assert salida.status_code == 200, salida.text

    assert salida.json()["porcentaje_avance"] == 100


def test_un_estado_insertado_como_dato_alarga_la_cadena_del_avance(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """EXTENSION POINT P3 aplicado al avance de RF-022.

    Insertar un paso nuevo en la tabla mueve la barra de progreso de todas las
    reservas sin tocar una línea de código, que es justo lo que no podría pasar
    si el ciclo estuviese escrito a mano en el servicio.
    """
    antes = estado_service.cadena_principal(db)
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id)
    avance_antes = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()["porcentaje_avance"]

    db.add(
        TransicionEstado(
            estado_origen="acabado",
            estado_destino="control_calidad",
            permiso_requerido="reserva:avanzar_estado",
            endpoint=None,
            marca_fin_servicio=False,
        )
    )
    db.add(
        TransicionEstado(
            estado_origen="control_calidad",
            estado_destino="finalizado",
            permiso_requerido="reserva:avanzar_estado",
            endpoint=None,
            marca_fin_servicio=False,
        )
    )
    db.commit()

    despues = estado_service.cadena_principal(db)
    assert len(despues) == len(antes) + 1
    assert "control_calidad" in despues

    avance_despues = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()["porcentaje_avance"]
    assert avance_despues < avance_antes, "el ciclo es más largo, así que falta más camino"


def test_una_reserva_cancelada_conserva_el_avance_que_alcanzo(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """Un estado fuera del flujo principal no borra el camino ya recorrido."""
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id)
    avance_antes = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()["porcentaje_avance"]

    cancelada = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/cancelacion", json={"motivo": "Cambio de planes"}
    )
    assert cancelada.status_code == 200, cancelada.text

    assert cancelada.json()["porcentaje_avance"] == avance_antes


# --------------------------------------------------------------------------
# La hora estimada de entrega
# --------------------------------------------------------------------------
def test_la_reserva_nace_con_su_hora_estimada_de_entrega(api_cliente, servicio_medio, vehiculo_id):
    """RF-022 paso 4: la estimación empieza siendo la que prometió la reserva."""
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id)

    assert reserva["hora_estimada_entrega"] == reserva["fin"]
    assert reserva["minutos_retraso"] == 0


def test_la_hora_estimada_se_recalcula_al_avanzar_el_servicio(
    api_cliente, api_recepcion, db, servicio_medio, vehiculo_id
):
    """El ingreso real manda sobre la hora reservada."""
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id)

    entrada = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": False}
    )
    assert entrada.status_code == 200, entrada.text

    fila = db.get(Reserva, reserva["id"])
    assert fila.hora_estimada_entrega is not None
    assert desde_bd(fila.hora_estimada_entrega) != desde_bd(fila.fin)


def test_un_servicio_puntual_no_genera_aviso_de_retraso(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """El aviso de 4a es la excepción, no el ruido de cada cambio de estado."""
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id)
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])

    assert _avisos_de_retraso(db, reserva["id"]) == []
    assert api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()["minutos_retraso"] == 0


def test_un_retraso_mayor_a_quince_minutos_avisa_con_el_nuevo_horario(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-022 flujo 4a.

    El bloque se mueve al pasado para que el servicio vaya tarde de verdad; el
    recálculo es puro respecto del reloj, así que no hace falta esperar.
    """
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id)

    fila = db.get(Reserva, reserva["id"])
    momento = ahora_utc()
    fila.inicio = momento - timedelta(hours=2)
    fila.fin = momento - timedelta(hours=1)
    fila.hora_estimada_entrega = fila.fin
    db.commit()

    seguimiento_service.actualizar_estimado(db, fila, momento=momento)
    db.commit()

    avisos = _avisos_de_retraso(db, reserva["id"])
    assert avisos, "un retraso de más de quince minutos tiene que avisarse"

    estimada = desde_bd(fila.hora_estimada_entrega)
    assert estimada - desde_bd(fila.fin) > seguimiento_service.UMBRAL_RETRASO
    # El aviso lleva el NUEVO horario, no un «vamos tarde» genérico.
    assert str(estimada.day) in avisos[0].cuerpo or "entrega" in avisos[0].cuerpo.lower()

    detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
    assert detalle["minutos_retraso"] > 15


def test_el_aviso_de_retraso_no_se_repite_si_la_estimacion_no_se_mueve(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """Recalcular con el mismo instante da el mismo horario: un solo aviso."""
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id)

    fila = db.get(Reserva, reserva["id"])
    momento = ahora_utc()
    fila.inicio = momento - timedelta(hours=2)
    fila.fin = momento - timedelta(hours=1)
    fila.hora_estimada_entrega = fila.fin
    db.commit()

    seguimiento_service.actualizar_estimado(db, fila, momento=momento)
    seguimiento_service.actualizar_estimado(db, fila, momento=momento)
    db.commit()

    assert len(_avisos_de_retraso(db, reserva["id"])) == 1


def test_el_umbral_de_aviso_es_de_quince_minutos():
    """RF-022 flujo 4a: el umbral es del requisito, no un número suelto."""
    assert seguimiento_service.UMBRAL_RETRASO == timedelta(minutes=15)
