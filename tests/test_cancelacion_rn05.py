"""RN-05 y el delta v1.0 de RF-016 - penalidad y reembolso al cancelar.

Cubre los dos CA que el SRS escribe para el delta:

* `CA-01` cancelación con **más de dos horas** de anticipación: penalidad cero;
* `CA-02` cancelación con **una hora** de anticipación: se retiene el **20 %**.

Y los dos pasos del delta que los acompañan:

* paso 5 «el sistema inicia el reembolso correspondiente **si el pago fue en
  línea**»;
* `5a` «falla el reembolso automático: el sistema registra la solicitud como
  **pendiente para gestión manual**».

Cierra el hueco del MVP: ``PoliticaSinPenalidad`` devolvía siempre cero.

Ninguna prueba depende de la hora real a la que se ejecute la suite: la reserva
se agenda un lunes a las 10:00 y el instante de la cancelación se fija con
``monkeypatch`` sobre el reloj que usa el servicio.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.horario import a_utc
from app.models import EstadoPago, EstadoReembolso, Pago, Reembolso, Reserva
from app.services import reserva_service
from app.services.politica_cancelacion import (
    ANTICIPACION_SIN_PENALIDAD,
    PORCENTAJE_PENALIDAD,
    PoliticaRN05,
    PoliticaSinPenalidad,
)
from tests.conftest import (
    RUTA,
    TARJETA_SIN_REVERSION,
    crear_reserva,
    instante,
    pagar_en_linea,
    proximo_lunes,
)

MONTO_ESPERADO = 2500
#: RN-05: el 20 % de S/ 25,00.
PENALIDAD_ESPERADA = 500


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _reserva(api_cliente, servicio, vehiculo_id, fecha, *, modalidad=None) -> dict:
    respuesta = crear_reserva(
        api_cliente,
        servicio.id,
        vehiculo_id,
        instante(fecha, 10, 0),
        modalidad_pago=modalidad,
    )
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _fijar_reloj(monkeypatch, fecha, hora: int, minuto: int = 0) -> None:
    """Congela el instante que ve ``reserva_service.cancelar``."""
    momento = a_utc(instante(fecha, hora, minuto))
    monkeypatch.setattr(reserva_service, "ahora_utc", lambda: momento)


def _cancelar(api, reserva_id: int, motivo: str = "Me surgió un imprevisto"):
    return api.post(f"{RUTA}/reservas/{reserva_id}/cancelacion", json={"motivo": motivo})


# --------------------------------------------------------------------------
# La política, leída como la escribe RN-05
# --------------------------------------------------------------------------
def test_la_politica_predeterminada_ya_no_es_la_que_devuelve_cero():
    """El hueco del MVP queda cerrado: la política por defecto es RN-05."""
    assert isinstance(reserva_service.POLITICA_PREDETERMINADA, PoliticaRN05)
    assert not isinstance(reserva_service.POLITICA_PREDETERMINADA, PoliticaSinPenalidad)
    assert ANTICIPACION_SIN_PENALIDAD == timedelta(hours=2)
    assert PORCENTAJE_PENALIDAD == 20


@pytest.mark.parametrize(
    ("horas_antes", "esperado"),
    [
        (24, 0),
        (3, 0),
        # "Más de dos horas" es estricto: dos horas exactas ya es tarde.
        (2, PENALIDAD_ESPERADA),
        (1, PENALIDAD_ESPERADA),
        (0, PENALIDAD_ESPERADA),
    ],
)
def test_la_penalidad_depende_de_la_anticipacion(
    api_cliente, db, servicio_medio, vehiculo_id, horas_antes, esperado
):
    """RN-05 en sus dos tramos, incluido el borde exacto de las dos horas."""
    fecha = proximo_lunes()
    reserva_json = _reserva(api_cliente, servicio_medio, vehiculo_id, fecha)
    reserva = db.get(Reserva, reserva_json["id"])
    momento = a_utc(instante(fecha, 10, 0)) - timedelta(hours=horas_antes)

    penalidad = PoliticaRN05().calcular_penalidad(reserva, momento)

    assert penalidad.monto_centimos == esperado
    assert penalidad.moneda == "PEN"


# --------------------------------------------------------------------------
# CA-01 y CA-02 por la puerta del API
# --------------------------------------------------------------------------
def test_cancelar_con_mas_de_dos_horas_no_tiene_penalidad(
    api_cliente, db, servicio_medio, vehiculo_id, monkeypatch
):
    """RF-016 v1.0 `CA-01`: «entonces la penalidad calculada es cero»."""
    fecha = proximo_lunes()
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id, fecha)
    _fijar_reloj(monkeypatch, fecha, 7, 0)  # tres horas antes

    respuesta = _cancelar(api_cliente, reserva["id"])

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "cancelada"
    assert cuerpo["penalidad"] == {"monto_centimos": 0, "moneda": "PEN"}
    assert db.get(Reserva, reserva["id"]).penalidad_centimos == 0


def test_cancelar_con_una_hora_retiene_el_20_por_ciento(
    api_cliente, db, servicio_medio, vehiculo_id, monkeypatch
):
    """RF-016 v1.0 `CA-02`: «entonces se retiene el 20 % del monto»."""
    fecha = proximo_lunes()
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id, fecha)
    assert reserva["monto"]["monto_centimos"] == MONTO_ESPERADO
    _fijar_reloj(monkeypatch, fecha, 9, 0)  # una hora antes

    respuesta = _cancelar(api_cliente, reserva["id"])

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["penalidad"] == {"monto_centimos": PENALIDAD_ESPERADA, "moneda": "PEN"}
    assert cuerpo["cancelacion"]["motivo"] == "Me surgió un imprevisto"

    # RF-016 paso 3: el resumen se puede volver a leer después.
    detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
    assert detalle["penalidad"] == {"monto_centimos": PENALIDAD_ESPERADA, "moneda": "PEN"}
    assert db.get(Reserva, reserva["id"]).penalidad_centimos == PENALIDAD_ESPERADA


# --------------------------------------------------------------------------
# Paso 5 - el reembolso del pago en línea
# --------------------------------------------------------------------------
def test_cancelar_un_pago_en_linea_devuelve_todo_cuando_no_hay_penalidad(
    api_cliente, db, servicio_medio, vehiculo_id, monkeypatch
):
    """RF-016 v1.0 paso 5: «inicia el reembolso correspondiente»."""
    fecha = proximo_lunes()
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id, fecha, modalidad="en_linea")
    assert pagar_en_linea(api_cliente, reserva["id"], clave="cobro-cancelado").status_code == 200
    _fijar_reloj(monkeypatch, fecha, 7, 0)

    respuesta = _cancelar(api_cliente, reserva["id"], "Cambio de planes")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["penalidad"]["monto_centimos"] == 0

    reembolso = db.scalars(select(Reembolso)).one()
    assert reembolso.estado == EstadoReembolso.PROCESADO.value
    assert reembolso.monto_centimos == MONTO_ESPERADO
    assert reembolso.tipo == "total"
    assert reserva["codigo"] in reembolso.motivo

    pago = db.scalars(select(Pago).where(Pago.reserva_id == reserva["id"])).one()
    assert pago.saldo_centimos == 0
    assert pago.estado == EstadoPago.REEMBOLSADO_TOTAL.value


def test_la_penalidad_se_descuenta_del_reembolso(
    api_cliente, db, servicio_medio, vehiculo_id, monkeypatch
):
    """RN-05 y el paso 5 juntos: se devuelve lo pagado MENOS lo que se retiene."""
    fecha = proximo_lunes()
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id, fecha, modalidad="en_linea")
    assert pagar_en_linea(api_cliente, reserva["id"], clave="cobro-tarde").status_code == 200
    _fijar_reloj(monkeypatch, fecha, 9, 0)  # una hora antes: 20 %

    respuesta = _cancelar(api_cliente, reserva["id"], "Ya no puedo llegar")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["penalidad"]["monto_centimos"] == PENALIDAD_ESPERADA

    reembolso = db.scalars(select(Reembolso)).one()
    assert reembolso.monto_centimos == MONTO_ESPERADO - PENALIDAD_ESPERADA == 2000
    assert reembolso.tipo == "parcial"

    pago = db.scalars(select(Pago).where(Pago.reserva_id == reserva["id"])).one()
    assert pago.saldo_centimos == PENALIDAD_ESPERADA, "el local se queda con la penalidad"
    assert pago.estado == EstadoPago.REEMBOLSADO_PARCIAL.value


def test_si_la_pasarela_rechaza_la_reversion_queda_pendiente_de_gestion_manual(
    api_cliente, db, servicio_medio, vehiculo_id, monkeypatch
):
    """RF-016 v1.0 `5a`: «registra la solicitud como pendiente para gestión manual».

    La cancelación se completa igual: perder la reserva porque la pasarela no
    contesta sería el peor de los dos resultados.
    """
    fecha = proximo_lunes()
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id, fecha, modalidad="en_linea")
    assert (
        pagar_en_linea(
            api_cliente, reserva["id"], tarjeta=TARJETA_SIN_REVERSION, clave="cobro-sin-reversion"
        ).status_code
        == 200
    )
    _fijar_reloj(monkeypatch, fecha, 7, 0)

    respuesta = _cancelar(api_cliente, reserva["id"], "Ya no lo necesito")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "cancelada", "la cancelación NO se revierte"

    reembolso = db.scalars(select(Reembolso)).one()
    assert reembolso.estado == EstadoReembolso.PENDIENTE_MANUAL.value
    assert reembolso.detalle
    pago = db.scalars(select(Pago).where(Pago.reserva_id == reserva["id"])).one()
    assert pago.saldo_centimos == MONTO_ESPERADO, "el dinero todavía no volvió"


def test_cancelar_una_reserva_presencial_no_inventa_un_reembolso(
    api_cliente, db, servicio_medio, vehiculo_id, monkeypatch
):
    """«Si el pago fue en línea»: sin cobro por pasarela no hay nada que revertir."""
    fecha = proximo_lunes()
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id, fecha)
    _fijar_reloj(monkeypatch, fecha, 9, 0)

    respuesta = _cancelar(api_cliente, reserva["id"])

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["penalidad"]["monto_centimos"] == PENALIDAD_ESPERADA
    assert db.scalars(select(Reembolso)).all() == []


def test_cancelar_desde_pendiente_de_pago_no_penaliza_ni_reembolsa(
    api_cliente, db, servicio_medio, vehiculo_id, monkeypatch
):
    """Precondición v1.0: «también cancelable desde Pendiente de pago»."""
    fecha = proximo_lunes()
    reserva = _reserva(api_cliente, servicio_medio, vehiculo_id, fecha, modalidad="en_linea")
    _fijar_reloj(monkeypatch, fecha, 7, 0)

    respuesta = _cancelar(api_cliente, reserva["id"], "Me equivoqué de día")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "cancelada"
    assert respuesta.json()["penalidad"]["monto_centimos"] == 0
    assert db.get(Reserva, reserva["id"]).expira_en is None, "ya no hay plazo que vencer"
    assert db.scalars(select(Reembolso)).all() == []
