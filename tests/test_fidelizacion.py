"""RF-032 - Programa de puntos de fidelización, y `RN-11`.

Cubre:

* ``CA-01`` un pago de **S/ 50,00** acredita **5 puntos**, al confirmarse;
* ``CA-02`` un canje con saldo insuficiente responde **422** diciendo cuántos
  puntos faltan, y un beneficio agotado o vencido **se retira del listado**
  (`4b`);
* `RN-11` «1 punto por cada S/ 10,00 **facturados**»: lo facturado es el
  ``total_centimos`` del desglose de INC-2, no el precio de catálogo;
* «100 puntos = un lavado básico sin costo»: el canje produce un **cupón** que
  entra por la misma puerta que cualquier otro, ``tarifa_service.calcular``.

El saldo no se guarda en ninguna columna: es la suma de ``puntos_movimiento``,
así que estas pruebas lo leen por el endpoint y nunca por un contador.
"""

from datetime import timedelta

from sqlalchemy import select

from app.core.horario import ahora, ahora_utc
from app.models import (
    CENTIMOS_POR_PUNTO,
    PUNTOS_LAVADO_BASICO,
    Beneficio,
    CuponCanje,
    EstadoCupon,
    EventoDominio,
    PuntosMovimiento,
)
from app.services import fidelizacion_service
from tests.conftest import (
    RUTA,
    TARJETA_APROBADA,
    codigo_error,
    crear_reserva,
    crear_vehiculo,
    entregar,
    instante,
    llevar_hasta_finalizado,
    pagar_en_caja,
    pagar_en_linea,
    proximo_lunes,
)

#: RF-032 CA-01, literal: un pago de S/ 50,00 acredita 5 puntos.
PAGO_CA01_CENTIMOS = 5000
PUNTOS_CA01 = 5


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _saldo(api) -> dict:
    respuesta = api.get(f"{RUTA}/fidelizacion/saldo")
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def _beneficios(api) -> list[dict]:
    respuesta = api.get(f"{RUTA}/fidelizacion/beneficios")
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()["items"]


def _beneficio_del_seed(db) -> Beneficio:
    fila = db.scalars(select(Beneficio)).first()
    assert fila is not None, "el seed debería dejar el beneficio de RN-11"
    return fila


def _regalar_puntos(db, usuario, puntos: int) -> None:
    """Put points in the account without walking a hundred services.

    The accrual itself is verified by CA-01; what these scenarios are about is
    what happens NEXT, so the balance is seeded through the same ledger the
    accrual writes - not through a counter, because there is not one.
    """
    fidelizacion_service.fidelizacion_repo.crear_movimiento(
        db,
        usuario_id=usuario.id,
        tipo="acumulacion",
        puntos=puntos,
        saldo_resultante=puntos,
        ocurrido_en=ahora_utc(),
    )
    db.commit()


def _servicio_pagado(
    api_cliente, api_recepcion, api_operario, db, servicio, vehiculo_id, *, monto=None
) -> dict:
    """Book, walk the whole chain and charge at the counter (RN-09)."""
    respuesta = crear_reserva(
        api_cliente, servicio.id, vehiculo_id, instante(proximo_lunes(dias_minimos=2), 10)
    )
    assert respuesta.status_code == 201, respuesta.text
    reserva = respuesta.json()

    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])
    cobro = pagar_en_caja(
        api_recepcion,
        reserva["id"],
        monto if monto is not None else reserva["monto"]["monto_centimos"],
    )
    assert cobro.status_code == 201, cobro.text
    return reserva


# --------------------------------------------------------------------------
# CA-01 - un pago de S/ 50,00 acredita 5 puntos
# --------------------------------------------------------------------------
def test_ca01_un_pago_de_cincuenta_soles_acredita_cinco_puntos(
    api_cliente, api_recepcion, api_operario, api_admin, db, servicio_corto, vehiculo_id
):
    """RF-032 `CA-01` con la cifra del requisito.

    El precio del servicio se lleva a S/ 50,00 antes de reservar para que lo
    FACTURADO sea exactamente esa cifra: `RN-11` cuenta sobre el total del
    desglose (INC-2), no sobre el precio de catálogo de otro día.
    """
    cambio = api_admin.patch(
        f"{RUTA}/admin/servicios/{servicio_corto.id}",
        json={"monto_centimos": PAGO_CA01_CENTIMOS},
    )
    assert cambio.status_code == 200, cambio.text

    antes = _saldo(api_cliente)["puntos"]
    reserva = _servicio_pagado(
        api_cliente, api_recepcion, api_operario, db, servicio_corto, vehiculo_id
    )
    assert reserva["monto"]["monto_centimos"] == PAGO_CA01_CENTIMOS

    cuerpo = _saldo(api_cliente)
    assert cuerpo["puntos"] - antes == PUNTOS_CA01
    assert cuerpo["centimos_por_punto"] == CENTIMOS_POR_PUNTO

    movimiento = cuerpo["movimientos"][0]
    assert movimiento["tipo"] == "acumulacion"
    assert movimiento["puntos"] == PUNTOS_CA01
    assert movimiento["reserva_id"] == reserva["id"]
    assert movimiento["base"]["monto_centimos"] == PAGO_CA01_CENTIMOS


def test_rn11_un_punto_por_cada_diez_soles_sin_redondear_hacia_arriba(
    api_cliente, api_recepcion, api_operario, api_admin, db, servicio_corto, vehiculo_id
):
    """`RN-11` dice «POR CADA S/ 10,00»: los soles sueltos no pagan un punto."""
    api_admin.patch(f"{RUTA}/admin/servicios/{servicio_corto.id}", json={"monto_centimos": 5990})

    _servicio_pagado(api_cliente, api_recepcion, api_operario, db, servicio_corto, vehiculo_id)

    assert _saldo(api_cliente)["puntos"] == 5


def test_lo_facturado_es_el_total_del_desglose_no_el_precio_de_catalogo(
    api_cliente, api_recepcion, api_operario, api_admin, db, servicio_corto, vehiculo_id
):
    """`RN-11` sobre lo que el cliente pagó de verdad (INC-2).

    Con un factor de vehículo de por medio, precio de catálogo y total del
    desglose son dos números distintos. Los puntos salen del segundo.
    """
    api_admin.patch(f"{RUTA}/admin/servicios/{servicio_corto.id}", json={"monto_centimos": 3000})
    # Una SUV: factor 1,3, el de RF-012 CA-01. 3000 -> 3900.
    suv = crear_vehiculo(api_cliente, "SUV-777")
    reserva = _servicio_pagado(api_cliente, api_recepcion, api_operario, db, servicio_corto, suv)

    assert reserva["tarifa"]["total"]["monto_centimos"] == 3900
    cuerpo = _saldo(api_cliente)
    assert cuerpo["puntos"] == 3, "3900 céntimos facturados = 3 puntos, no 3000 -> 3"
    assert cuerpo["movimientos"][0]["base"]["monto_centimos"] == 3900


def test_la_acumulacion_ocurre_al_confirmarse_el_pago_no_al_reservar(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-032: «acumulación AL CONFIRMARSE EL PAGO»."""
    respuesta = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(dias_minimos=2), 10)
    )
    reserva = respuesta.json()

    assert _saldo(api_cliente)["puntos"] == 0, "reservar no acredita nada"

    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])
    assert _saldo(api_cliente)["puntos"] == 0, "terminar el servicio tampoco"

    pagar_en_caja(api_recepcion, reserva["id"], reserva["monto"]["monto_centimos"])
    assert _saldo(api_cliente)["puntos"] > 0, "el pago confirmado sí"


def test_el_pago_en_linea_acredita_por_la_misma_puerta(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """Una sola regla para las dos puertas de cobro (RF-025 / RF-026)."""
    respuesta = crear_reserva(
        api_cliente,
        servicio_medio.id,
        vehiculo_id,
        instante(proximo_lunes(dias_minimos=2), 10),
        modalidad_pago="en_linea",
    )
    reserva = respuesta.json()

    cobro = pagar_en_linea(api_cliente, reserva["id"], tarjeta=TARJETA_APROBADA)
    assert cobro.status_code == 200, cobro.text
    assert cobro.json()["estado_pago"] == "confirmado"

    assert _saldo(api_cliente)["puntos"] == reserva["monto"]["monto_centimos"] // CENTIMOS_POR_PUNTO


def test_un_pago_rechazado_no_acredita_puntos(api_cliente, db, servicio_medio, vehiculo_id):
    """Nada facturado, nada acumulado (RF-026 `3a`)."""
    from tests.conftest import TARJETA_RECHAZADA

    respuesta = crear_reserva(
        api_cliente,
        servicio_medio.id,
        vehiculo_id,
        instante(proximo_lunes(dias_minimos=2), 10),
        modalidad_pago="en_linea",
    )
    reserva = respuesta.json()

    cobro = pagar_en_linea(api_cliente, reserva["id"], tarjeta=TARJETA_RECHAZADA)
    assert cobro.status_code == 422

    assert _saldo(api_cliente)["puntos"] == 0


def test_reintentar_el_cobro_con_la_misma_clave_no_acredita_dos_veces(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-026 `CA-02` reproduce un cobro; `RN-11` no puede pagar dos veces.

    La idempotencia está en la base (``uq_puntos_movimiento_pago``), no sólo en
    una comprobación que alguien pueda quitar.
    """
    reserva = _servicio_pagado(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    esperado = _saldo(api_cliente)["puntos"]

    repetido = pagar_en_caja(
        api_recepcion, reserva["id"], reserva["monto"]["monto_centimos"], clave="caja-1"
    )
    assert repetido.status_code == 200, "misma clave: se reproduce el pago"

    assert _saldo(api_cliente)["puntos"] == esperado
    movimientos = list(db.scalars(select(PuntosMovimiento)).all())
    assert len(movimientos) == 1


# --------------------------------------------------------------------------
# CA-02 - saldo insuficiente
# --------------------------------------------------------------------------
def test_ca02_canjear_sin_saldo_responde_422_con_los_puntos_que_faltan(
    api_cliente, db, usuario_cliente
):
    """RF-032 `CA-02` y `4a`: «se informan los puntos faltantes»."""
    beneficio = _beneficio_del_seed(db)
    _regalar_puntos(db, usuario_cliente, 40)

    respuesta = api_cliente.post(f"{RUTA}/fidelizacion/canjes", json={"beneficio_id": beneficio.id})

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "PUNTOS_INSUFICIENTES"
    detalles = {item["campo"]: item["mensaje"] for item in respuesta.json()["error"]["detalles"]}
    assert detalles["puntos_faltantes"] == str(PUNTOS_LAVADO_BASICO - 40)
    assert "te faltan 60" in detalles["beneficio_id"]


def test_el_listado_dice_cuantos_puntos_faltan_antes_de_intentarlo(
    api_cliente, db, usuario_cliente
):
    """La pantalla no tiene que restar por su cuenta."""
    _regalar_puntos(db, usuario_cliente, 25)

    beneficio = _beneficios(api_cliente)[0]

    assert beneficio["alcanzable"] is False
    assert beneficio["puntos_faltantes"] == PUNTOS_LAVADO_BASICO - 25
    assert _saldo(api_cliente)["puntos_para_el_siguiente"] == PUNTOS_LAVADO_BASICO - 25


# --------------------------------------------------------------------------
# 4b - un beneficio agotado o vencido se retira del listado
# --------------------------------------------------------------------------
def test_4b_un_beneficio_agotado_se_retira_del_listado(api_cliente, db):
    """RF-032 `4b`, primera mitad: el stock llegó a cero."""
    beneficio = _beneficio_del_seed(db)
    assert _beneficios(api_cliente)

    beneficio.stock = 0
    db.commit()

    assert _beneficios(api_cliente) == []


def test_4b_un_beneficio_vencido_se_retira_del_listado(api_cliente, db):
    """RF-032 `4b`, segunda mitad: nadie lo barre, deja de coincidir solo."""
    beneficio = _beneficio_del_seed(db)
    beneficio.vigente_hasta = ahora().date() - timedelta(days=1)
    db.commit()

    assert _beneficios(api_cliente) == []


def test_canjear_un_beneficio_retirado_responde_422(api_cliente, db, usuario_cliente):
    """Quien tenía el listado viejo abierto recibe una explicación, no un 404."""
    beneficio = _beneficio_del_seed(db)
    _regalar_puntos(db, usuario_cliente, PUNTOS_LAVADO_BASICO)
    beneficio.stock = 0
    db.commit()

    respuesta = api_cliente.post(f"{RUTA}/fidelizacion/canjes", json={"beneficio_id": beneficio.id})

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "BENEFICIO_NO_DISPONIBLE"


# --------------------------------------------------------------------------
# 100 puntos = un lavado básico sin costo
# --------------------------------------------------------------------------
def test_cien_puntos_canjean_un_lavado_basico_sin_costo(
    api_cliente, db, usuario_cliente, servicio_corto
):
    """`RN-11`: el canje entrega un cupón y la reserva con él sale a cero.

    El cupón no es un descuento aparte: es una ``promocion`` con su código, así
    que el importe lo sigue calculando `RF-012` y el desglose lo explica como
    explica cualquier otro cupón (`RN-04` tiene un solo autor).
    """
    beneficio = _beneficio_del_seed(db)
    _regalar_puntos(db, usuario_cliente, PUNTOS_LAVADO_BASICO)

    canje = api_cliente.post(f"{RUTA}/fidelizacion/canjes", json={"beneficio_id": beneficio.id})
    assert canje.status_code == 201, canje.text
    cupon = canje.json()
    assert cupon["estado"] == EstadoCupon.EMITIDO.value
    assert cupon["beneficio"] == beneficio.nombre

    reserva = crear_reserva(
        api_cliente,
        servicio_corto.id,
        _vehiculo_id_de(api_cliente),
        instante(proximo_lunes(dias_minimos=2), 10),
    )
    # El cupón viaja por el campo ``cupon`` de siempre.
    reserva = api_cliente.post(
        f"{RUTA}/reservas",
        json={
            "servicio_id": servicio_corto.id,
            "vehiculo_id": _vehiculo_id_de(api_cliente),
            "inicio": instante(proximo_lunes(dias_minimos=2), 12).isoformat(),
            "cupon": cupon["codigo"],
        },
    )
    assert reserva.status_code == 201, reserva.text
    cuerpo = reserva.json()

    assert cuerpo["monto"]["monto_centimos"] == 0, "un lavado básico SIN COSTO"
    assert cuerpo["tarifa"]["cupon_aplicado"] == cupon["codigo"]
    assert cuerpo["tarifa"]["cupon_rechazado"] is None


def _vehiculo_id_de(api) -> int:
    return api.get(f"{RUTA}/vehiculos").json()["items"][0]["id"]


def test_el_canje_descuenta_los_puntos_y_deja_su_movimiento(api_cliente, db, usuario_cliente):
    beneficio = _beneficio_del_seed(db)
    _regalar_puntos(db, usuario_cliente, 150)

    api_cliente.post(f"{RUTA}/fidelizacion/canjes", json={"beneficio_id": beneficio.id})

    cuerpo = _saldo(api_cliente)
    assert cuerpo["puntos"] == 150 - PUNTOS_LAVADO_BASICO
    canjes = [item for item in cuerpo["movimientos"] if item["tipo"] == "canje"]
    assert canjes[0]["puntos"] == -PUNTOS_LAVADO_BASICO
    assert canjes[0]["beneficio"] == beneficio.nombre
    assert cuerpo["cupones"][0]["estado"] == EstadoCupon.EMITIDO.value


def test_un_cupon_de_canje_solo_sirve_una_vez(api_cliente, db, usuario_cliente, servicio_corto):
    """El cupón se gasta con la reserva que lo aprovechó.

    Se marca DESPUÉS de congelar el desglose, así que sólo se gasta si de
    verdad descontó; un cupón rechazado sigue ahí para el siguiente intento.
    """
    beneficio = _beneficio_del_seed(db)
    _regalar_puntos(db, usuario_cliente, PUNTOS_LAVADO_BASICO)
    cupon = api_cliente.post(
        f"{RUTA}/fidelizacion/canjes", json={"beneficio_id": beneficio.id}
    ).json()
    vehiculo = _vehiculo_id_de(api_cliente)
    fecha = proximo_lunes(dias_minimos=2)

    primera = api_cliente.post(
        f"{RUTA}/reservas",
        json={
            "servicio_id": servicio_corto.id,
            "vehiculo_id": vehiculo,
            "inicio": instante(fecha, 10).isoformat(),
            "cupon": cupon["codigo"],
        },
    )
    assert primera.status_code == 201, primera.text
    assert primera.json()["monto"]["monto_centimos"] == 0

    segunda = api_cliente.post(
        f"{RUTA}/reservas",
        json={
            "servicio_id": servicio_corto.id,
            "vehiculo_id": vehiculo,
            "inicio": instante(fecha, 12).isoformat(),
            "cupon": cupon["codigo"],
        },
    )

    assert segunda.status_code == 201, "RF-012 `3a`: el cupón malo no tumba la reserva"
    cuerpo = segunda.json()
    assert cuerpo["monto"]["monto_centimos"] > 0
    assert cuerpo["tarifa"]["cupon_rechazado"] == cupon["codigo"]

    fila = db.scalars(select(CuponCanje).where(CuponCanje.codigo == cupon["codigo"])).first()
    assert fila.estado == EstadoCupon.USADO.value
    assert fila.reserva_id == primera.json()["id"]


def test_un_cupon_de_canje_ajeno_se_rechaza_con_su_motivo(
    api_cliente, api_recepcion, db, usuario_cliente, servicio_corto
):
    """Los puntos son de una persona, y el cupón que producen también.

    El rechazo viaja por el mismo sitio que el de un cupón vencido (`RF-012`
    `3a`): se informa el motivo y el total se recalcula sin el descuento.
    """
    beneficio = _beneficio_del_seed(db)
    _regalar_puntos(db, usuario_cliente, PUNTOS_LAVADO_BASICO)
    cupon = api_cliente.post(
        f"{RUTA}/fidelizacion/canjes", json={"beneficio_id": beneficio.id}
    ).json()

    cotizacion = api_recepcion.post(
        f"{RUTA}/tarifas/calculo",
        json={
            "servicio_id": servicio_corto.id,
            "tipo_vehiculo": "sedan",
            "cupon": cupon["codigo"],
        },
    )

    assert cotizacion.status_code == 200, cotizacion.text
    desglose = cotizacion.json()
    assert desglose["cupon_rechazado"] == cupon["codigo"]
    assert "otra cuenta" in desglose["motivo_rechazo_cupon"]
    assert desglose["descuento"]["monto_centimos"] == 0


def test_el_canje_queda_en_la_bitacora_de_eventos(api_cliente, db, usuario_cliente):
    """P7: acumular y canjear son operaciones sensibles."""
    beneficio = _beneficio_del_seed(db)
    _regalar_puntos(db, usuario_cliente, PUNTOS_LAVADO_BASICO)

    api_cliente.post(f"{RUTA}/fidelizacion/canjes", json={"beneficio_id": beneficio.id})

    acciones = {
        fila.accion
        for fila in db.scalars(
            select(EventoDominio).where(EventoDominio.accion.like("puntos.%"))
        ).all()
    }
    assert "puntos.canjeados" in acciones


def test_la_acumulacion_queda_en_la_bitacora_de_eventos(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    _servicio_pagado(api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id)

    acciones = {
        fila.accion
        for fila in db.scalars(
            select(EventoDominio).where(EventoDominio.accion.like("puntos.%"))
        ).all()
    }
    assert "puntos.acumulados" in acciones


# --------------------------------------------------------------------------
# P5 - autorización por código de permiso
# --------------------------------------------------------------------------
def test_el_operario_no_ve_el_programa_de_puntos(api_operario):
    """Los puntos son del cliente; el permiso es lo que lo dice (P5)."""
    assert api_operario.get(f"{RUTA}/fidelizacion/saldo").status_code == 403
    assert api_operario.get(f"{RUTA}/fidelizacion/beneficios").status_code == 403


def test_el_saldo_exige_sesion(cliente_http):
    assert cliente_http.get(f"{RUTA}/fidelizacion/saldo").status_code == 401


def test_cada_cliente_ve_su_propio_saldo(api_cliente, api_admin, db, usuario_cliente):
    """No hay endpoint de «puntos de otro»: la identidad es el token."""
    _regalar_puntos(db, usuario_cliente, 30)

    assert _saldo(api_cliente)["puntos"] == 30
    assert _saldo(api_admin)["puntos"] == 0


# --------------------------------------------------------------------------
# La entrega del servicio no cambia: los puntos son un efecto del pago
# --------------------------------------------------------------------------
def test_acumular_puntos_no_estorba_la_entrega(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """`RN-09` sigue igual: el servicio se entrega con el pago confirmado."""
    reserva = _servicio_pagado(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )

    respuesta = entregar(api_recepcion, reserva["id"])

    assert respuesta.status_code == 200, respuesta.text
    assert _saldo(api_cliente)["puntos"] > 0
