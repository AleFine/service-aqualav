"""RF-028 - Gestión de anulaciones y reembolsos.

Cubre:

* `CA-01` un reembolso parcial válido **reduce el saldo del pago** en ese monto;
* `CA-02` un reembolso mayor al monto pagado responde **422**;
* `3a` si la pasarela rechaza la reversión, la solicitud **queda registrada
  como pendiente de gestión manual** (no se pierde);
* `RNF-017` M1 la **clave de idempotencia es obligatoria también aquí**.

Cierra además dos huecos del MVP: ``EstadoPago.ANULADO`` deja de estar sin
usar, y ``Pago.referencia_externa`` guarda por fin el identificador de la
pasarela que la reversión necesita.
"""

from sqlalchemy import select

from app.models import EstadoPago, EstadoReembolso, EventoDominio, Pago, Reembolso
from tests.conftest import (
    RUTA,
    TARJETA_APROBADA,
    TARJETA_SIN_REVERSION,
    codigo_error,
    crear_reserva,
    instante,
    llevar_hasta_finalizado,
    pagar_en_linea,
    proximo_lunes,
)

MONTO_ESPERADO = 2500


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _pago_en_linea(api_cliente, db, servicio, vehiculo_id, *, tarjeta=TARJETA_APROBADA) -> Pago:
    """Una reserva pagada por la pasarela, lista para revertirse."""
    reserva = crear_reserva(
        api_cliente,
        servicio.id,
        vehiculo_id,
        instante(proximo_lunes(), 10, 0),
        modalidad_pago="en_linea",
    ).json()
    respuesta = pagar_en_linea(api_cliente, reserva["id"], tarjeta=tarjeta, clave="cobro-previo")
    assert respuesta.status_code == 200, respuesta.text
    return db.scalars(select(Pago).where(Pago.reserva_id == reserva["id"])).one()


def _pago_en_caja(api_cliente, api_recepcion, api_operario, db, servicio, vehiculo_id) -> Pago:
    reserva = crear_reserva(
        api_cliente, servicio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    ).json()
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])
    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/pagos",
        json={"medio": "efectivo", "monto_centimos": MONTO_ESPERADO},
        headers={"Idempotency-Key": "cobro-caja-previo"},
    )
    assert respuesta.status_code == 201, respuesta.text
    return db.scalars(select(Pago).where(Pago.reserva_id == reserva["id"])).one()


def _reembolsar(
    api_admin,
    pago_id,
    *,
    tipo="parcial",
    monto=None,
    motivo="Servicio incompleto",
    clave="reembolso-1",
):
    cuerpo = {"tipo": tipo, "motivo": motivo}
    if monto is not None:
        cuerpo["monto_centimos"] = monto
    return api_admin.post(
        f"{RUTA}/pagos/{pago_id}/reembolsos",
        json=cuerpo,
        headers={"Idempotency-Key": clave},
    )


# --------------------------------------------------------------------------
# CA-01 - el reembolso parcial reduce el saldo
# --------------------------------------------------------------------------
def test_un_reembolso_parcial_reduce_el_saldo_del_pago(
    api_cliente, api_admin, db, servicio_medio, vehiculo_id
):
    """RF-028 `CA-01`: «el saldo del pago se reduce en ese monto»."""
    pago = _pago_en_linea(api_cliente, db, servicio_medio, vehiculo_id)
    assert pago.saldo_centimos == MONTO_ESPERADO

    respuesta = _reembolsar(api_admin, pago.id, monto=1000)

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == EstadoReembolso.PROCESADO.value
    assert cuerpo["monto"] == {"monto_centimos": 1000, "moneda": "PEN"}
    assert cuerpo["referencia_externa"], "la pasarela devolvió su identificador"
    assert cuerpo["autor"]

    db.refresh(pago)
    assert pago.saldo_centimos == MONTO_ESPERADO - 1000
    assert pago.estado == EstadoPago.REEMBOLSADO_PARCIAL.value


def test_un_reembolso_total_deja_el_pago_reembolsado(
    api_cliente, api_admin, db, servicio_medio, vehiculo_id
):
    pago = _pago_en_linea(api_cliente, db, servicio_medio, vehiculo_id)

    respuesta = _reembolsar(api_admin, pago.id, tipo="total", motivo="El local no abrió")

    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["monto"]["monto_centimos"] == MONTO_ESPERADO
    db.refresh(pago)
    assert pago.saldo_centimos == 0
    assert pago.estado == EstadoPago.REEMBOLSADO_TOTAL.value


def test_la_anulacion_deja_el_pago_anulado(api_cliente, api_admin, db, servicio_medio, vehiculo_id):
    """Cierra el hueco del MVP: ``EstadoPago.ANULADO`` deja de estar sin usar."""
    pago = _pago_en_linea(api_cliente, db, servicio_medio, vehiculo_id)

    respuesta = _reembolsar(api_admin, pago.id, tipo="anulacion", motivo="Cobro duplicado")

    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["tipo"] == "anulacion"
    db.refresh(pago)
    assert pago.estado == EstadoPago.ANULADO.value
    assert pago.saldo_centimos == 0


# --------------------------------------------------------------------------
# CA-02 - más de lo pagado
# --------------------------------------------------------------------------
def test_un_reembolso_mayor_al_pagado_responde_422(
    api_cliente, api_admin, db, servicio_medio, vehiculo_id
):
    """RF-028 `CA-02` y flujo `2a`."""
    pago = _pago_en_linea(api_cliente, db, servicio_medio, vehiculo_id)

    respuesta = _reembolsar(api_admin, pago.id, monto=MONTO_ESPERADO + 1)

    assert respuesta.status_code == 422, respuesta.text
    assert codigo_error(respuesta) == "MONTO_MAYOR_AL_PAGADO"
    mensajes = [detalle["mensaje"] for detalle in respuesta.json()["error"]["detalles"]]
    assert any(str(MONTO_ESPERADO) in mensaje for mensaje in mensajes), "se informa el saldo"

    db.refresh(pago)
    assert pago.saldo_centimos == MONTO_ESPERADO, "nada se movió"
    assert db.scalars(select(Reembolso)).all() == []


def test_dos_parciales_no_pueden_superar_el_saldo(
    api_cliente, api_admin, db, servicio_medio, vehiculo_id
):
    """El límite es el SALDO, no el monto original."""
    pago = _pago_en_linea(api_cliente, db, servicio_medio, vehiculo_id)
    assert _reembolsar(api_admin, pago.id, monto=2000, clave="parcial-1").status_code == 201

    segundo = _reembolsar(api_admin, pago.id, monto=1000, clave="parcial-2")

    assert segundo.status_code == 422
    assert codigo_error(segundo) == "MONTO_MAYOR_AL_PAGADO"
    db.refresh(pago)
    assert pago.saldo_centimos == 500


# --------------------------------------------------------------------------
# 3a - la pasarela rechaza la reversión
# --------------------------------------------------------------------------
def test_la_reversion_rechazada_queda_pendiente_de_gestion_manual(
    api_cliente, api_admin, db, servicio_medio, vehiculo_id
):
    """RF-028 `3a`: la solicitud **no se pierde**, queda esperando a una persona.

    La tarjeta ``4222`` aprueba el cobro y rechaza la reversión, que es
    exactamente el escenario que el flujo describe.
    """
    pago = _pago_en_linea(
        api_cliente, db, servicio_medio, vehiculo_id, tarjeta=TARJETA_SIN_REVERSION
    )

    respuesta = _reembolsar(api_admin, pago.id, monto=1500, motivo="Reclamo del cliente")

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == EstadoReembolso.PENDIENTE_MANUAL.value
    assert cuerpo["detalle"], "queda escrito por qué la pasarela dijo que no"
    assert cuerpo["referencia_externa"] is None

    # El saldo NO se movió: el dinero todavía no volvió.
    db.refresh(pago)
    assert pago.saldo_centimos == MONTO_ESPERADO
    assert pago.estado == EstadoPago.CONFIRMADO.value

    # Y la solicitud es consultable, que es lo que la hace gestionable.
    listado = api_admin.get(f"{RUTA}/pagos/{pago.id}/reembolsos")
    assert listado.status_code == 200, listado.text
    assert [fila["estado"] for fila in listado.json()["items"]] == ["pendiente_manual"]

    acciones = {evento.accion for evento in db.scalars(select(EventoDominio)).all()}
    assert "reembolso.pendiente_manual" in acciones


# --------------------------------------------------------------------------
# RNF-017 M1 - idempotencia obligatoria
# --------------------------------------------------------------------------
def test_el_reembolso_exige_clave_de_idempotencia(
    api_cliente, api_admin, db, servicio_medio, vehiculo_id
):
    """RNF-017 M1: «clave de idempotencia obligatoria también en reembolsos»."""
    pago = _pago_en_linea(api_cliente, db, servicio_medio, vehiculo_id)

    respuesta = api_admin.post(
        f"{RUTA}/pagos/{pago.id}/reembolsos",
        json={"tipo": "parcial", "monto_centimos": 500, "motivo": "Sin clave"},
    )

    assert respuesta.status_code == 400
    assert codigo_error(respuesta) == "IDEMPOTENCY_KEY_REQUERIDA"
    assert db.scalars(select(Reembolso)).all() == []


def test_repetir_la_clave_no_devuelve_el_dinero_dos_veces(
    api_cliente, api_admin, db, servicio_medio, vehiculo_id
):
    pago = _pago_en_linea(api_cliente, db, servicio_medio, vehiculo_id)

    primero = _reembolsar(api_admin, pago.id, monto=800, clave="idem-1")
    segundo = _reembolsar(api_admin, pago.id, monto=800, clave="idem-1")

    assert primero.status_code == 201, primero.text
    assert segundo.status_code == 200, segundo.text
    assert segundo.json()["id"] == primero.json()["id"]
    assert len(db.scalars(select(Reembolso)).all()) == 1
    db.refresh(pago)
    assert pago.saldo_centimos == MONTO_ESPERADO - 800


# --------------------------------------------------------------------------
# Bordes y permisos
# --------------------------------------------------------------------------
def test_un_pago_de_caja_se_devuelve_en_caja(
    api_cliente, api_admin, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """Sin pasarela detrás no hay reversión que pedir: la devuelve el mostrador."""
    pago = _pago_en_caja(api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id)

    respuesta = _reembolsar(api_admin, pago.id, monto=500, motivo="Devolución en caja")

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == EstadoReembolso.PROCESADO.value
    assert cuerpo["referencia_externa"] is None
    assert "caja" in cuerpo["detalle"]
    db.refresh(pago)
    assert pago.saldo_centimos == MONTO_ESPERADO - 500


def test_solo_quien_tiene_pago_reembolsar_puede_pedirlo(
    api_cliente, api_recepcion, db, servicio_medio, vehiculo_id
):
    """P5: la autorización es por permiso, y ``pago:reembolsar`` es del administrador."""
    pago = _pago_en_linea(api_cliente, db, servicio_medio, vehiculo_id)

    del_cliente = _reembolsar(api_cliente, pago.id, monto=100, clave="del-cliente")
    del_mostrador = _reembolsar(api_recepcion, pago.id, monto=100, clave="del-mostrador")

    assert del_cliente.status_code == 403
    assert del_mostrador.status_code == 403


def test_un_pago_inexistente_responde_404(api_admin):
    respuesta = _reembolsar(api_admin, 9999, monto=100, clave="fantasma")

    assert respuesta.status_code == 404
    assert codigo_error(respuesta) == "RECURSO_NO_ENCONTRADO"
