"""RF-017 delta v1.0 - filtros del historial de reservas.

Cubre:

* ``CA-02`` «dado un filtro por vehículo, solo se listan las reservas de ese
  vehículo»;
* el filtro por **rango de fechas**;
* el **filtro multi-estado**, que es el hueco del plan §7: hoy
  ``(cliente)/inicio.tsx`` y ``(personal)/operacion.tsx`` hacen una petición
  por estado activo porque la API no sabía responder por varios a la vez;
* que el detalle enlaza **comprobante** y **evidencias**, que es el paso 4 del
  flujo v1.0;
* que ``CA-01`` (paginación de 20) y ``CA-03`` (aislamiento entre clientes)
  siguen valiendo con los filtros nuevos encima.

El filtro de estado **no** se valida contra un catálogo: la máquina vive en
``transicion_estado`` (P3), así que un estado insertado como dato se puede
filtrar la misma tarde y uno inexistente simplemente no coincide con nada.
"""

from datetime import timedelta

from tests.conftest import (
    MIME_PNG,
    PIXEL_PNG_BASE64,
    RUTA,
    codigo_error,
    crear_reserva,
    crear_vehiculo,
    instante,
    pagar_en_caja,
    proximo_lunes,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _listar(api, **params):
    return api.get(f"{RUTA}/reservas", params=params)


def _codigos(respuesta) -> set[str]:
    return {item["codigo"] for item in respuesta.json()["items"]}


def _reservar(api, servicio, vehiculo_id, fecha, hora: int) -> dict:
    respuesta = crear_reserva(api, servicio.id, vehiculo_id, instante(fecha, hora))
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


# --------------------------------------------------------------------------
# CA-02 - filtro por vehículo
# --------------------------------------------------------------------------
def test_ca02_el_filtro_por_vehiculo_solo_lista_ese_vehiculo(
    api_cliente, servicio_corto, vehiculo_id
):
    """RF-017 `CA-02`, literal."""
    fecha = proximo_lunes(dias_minimos=2)
    otro = crear_vehiculo(api_cliente, "XYZ-987")
    del_primero = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 9)
    del_segundo = _reservar(api_cliente, servicio_corto, otro, fecha, 11)

    respuesta = _listar(api_cliente, vehiculo_id=otro)

    assert respuesta.status_code == 200, respuesta.text
    assert _codigos(respuesta) == {del_segundo["codigo"]}
    assert del_primero["codigo"] not in _codigos(respuesta)
    assert respuesta.json()["total"] == 1


def test_el_filtro_por_vehiculo_no_abre_la_puerta_al_vehiculo_de_otro(
    api_cliente, api_admin, servicio_corto, vehiculo_id
):
    """`CA-03` sigue mandando: el filtro se aplica DENTRO de lo que ya se ve."""
    fecha = proximo_lunes(dias_minimos=2)
    _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 9)

    ajeno = crear_vehiculo(api_admin, "ADM-111")
    respuesta = _listar(api_cliente, vehiculo_id=ajeno)

    assert respuesta.status_code == 200
    assert respuesta.json()["items"] == []


# --------------------------------------------------------------------------
# Rango de fechas
# --------------------------------------------------------------------------
def test_el_rango_de_fechas_acota_por_el_inicio_de_la_reserva(
    api_cliente, servicio_corto, vehiculo_id
):
    """El delta v1.0 pide «rango de fechas», y el día es de Lima, no de UTC."""
    primera = proximo_lunes(dias_minimos=2)
    segunda = primera + timedelta(days=7)
    de_la_primera = _reservar(api_cliente, servicio_corto, vehiculo_id, primera, 10)
    de_la_segunda = _reservar(api_cliente, servicio_corto, vehiculo_id, segunda, 10)

    respuesta = _listar(api_cliente, desde=segunda.isoformat(), hasta=segunda.isoformat())

    assert _codigos(respuesta) == {de_la_segunda["codigo"]}
    assert de_la_primera["codigo"] not in _codigos(respuesta)


def test_el_rango_incluye_los_dos_extremos(api_cliente, servicio_corto, vehiculo_id):
    """``hasta`` es inclusivo para el cliente: un día entero, no hasta su 00:00."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 18)

    respuesta = _listar(api_cliente, desde=fecha.isoformat(), hasta=fecha.isoformat())

    assert _codigos(respuesta) == {reserva["codigo"]}


def test_un_rango_invertido_se_rechaza_explicando_el_motivo(api_cliente):
    fecha = proximo_lunes(dias_minimos=2)

    respuesta = _listar(
        api_cliente,
        desde=fecha.isoformat(),
        hasta=(fecha - timedelta(days=3)).isoformat(),
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "DATOS_INVALIDOS"


def test_los_filtros_se_combinan(api_cliente, servicio_corto, vehiculo_id):
    """Vehículo, rango y estado juntos: una sola consulta, no tres."""
    fecha = proximo_lunes(dias_minimos=2)
    otro = crear_vehiculo(api_cliente, "MIX-321")
    esperada = _reservar(api_cliente, servicio_corto, otro, fecha, 10)
    _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 12)
    _reservar(api_cliente, servicio_corto, otro, fecha + timedelta(days=7), 10)

    respuesta = _listar(
        api_cliente,
        vehiculo_id=otro,
        desde=fecha.isoformat(),
        hasta=fecha.isoformat(),
        estado="confirmada",
    )

    assert _codigos(respuesta) == {esperada["codigo"]}


# --------------------------------------------------------------------------
# El hueco del plan §7 - filtro multi-estado
# --------------------------------------------------------------------------
def test_el_filtro_de_estado_admite_varios_valores(
    api_cliente, api_recepcion, api_operario, db, servicio_corto, vehiculo_id
):
    """El hueco de §7: una petición por estado activo pasa a ser UNA.

    Tres reservas en tres estados distintos, y la pantalla de agregado pide los
    dos que le interesan en una sola llamada.
    """
    fecha = proximo_lunes(dias_minimos=2)
    confirmada = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 9)
    recibida = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 11)
    lavando = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 13)

    api_recepcion.post(
        f"{RUTA}/reservas/{recibida['id']}/check-in", json={"confirmar_retraso": True}
    )
    api_recepcion.post(
        f"{RUTA}/reservas/{lavando['id']}/check-in", json={"confirmar_retraso": True}
    )
    api_recepcion.post(f"{RUTA}/reservas/{lavando['id']}/asignacion", json={})
    api_operario.post(f"{RUTA}/reservas/{lavando['id']}/estado", json={"estado": "en_lavado"})

    respuesta = api_cliente.get(
        f"{RUTA}/reservas", params=[("estado", "en_recepcion"), ("estado", "en_lavado")]
    )

    assert respuesta.status_code == 200, respuesta.text
    assert _codigos(respuesta) == {recibida["codigo"], lavando["codigo"]}
    assert confirmada["codigo"] not in _codigos(respuesta)
    assert respuesta.json()["total"] == 2


def test_un_solo_estado_se_comporta_como_antes(api_cliente, servicio_corto, vehiculo_id):
    """La forma del MVP sigue valiendo: el cliente móvil no tiene que cambiar."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)
    api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/cancelacion", json={"motivo": "Cambio de planes"}
    )
    otra = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 12)

    respuesta = _listar(api_cliente, estado="confirmada")

    assert _codigos(respuesta) == {otra["codigo"]}


def test_un_estado_inexistente_no_coincide_con_nada_y_no_es_un_error(
    api_cliente, servicio_corto, vehiculo_id
):
    """P3: el filtro no valida contra un catálogo de estados escrito en código.

    Validarlo obligaría a enumerarlos en la frontera de la API, que es
    exactamente lo que el punto de extensión prohíbe. Un estado que nadie
    insertó simplemente no coincide, que es la respuesta honesta.
    """
    fecha = proximo_lunes(dias_minimos=2)
    _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)

    respuesta = _listar(api_cliente, estado="estado_que_nadie_inserto")

    assert respuesta.status_code == 200
    assert respuesta.json()["items"] == []


def test_sin_filtro_de_estado_se_listan_todas(api_cliente, servicio_corto, vehiculo_id):
    fecha = proximo_lunes(dias_minimos=2)
    primera = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)
    segunda = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 12)
    api_cliente.post(
        f"{RUTA}/reservas/{primera['id']}/cancelacion", json={"motivo": "Cambio de planes"}
    )

    respuesta = _listar(api_cliente)

    assert _codigos(respuesta) == {primera["codigo"], segunda["codigo"]}


# --------------------------------------------------------------------------
# Paso 4 del flujo v1.0 - el detalle enlaza comprobante y evidencias
# --------------------------------------------------------------------------
def test_el_detalle_enlaza_el_comprobante_y_las_evidencias(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-017 paso 4: «el cliente abre una reserva y consulta su detalle,
    comprobante y evidencias».

    El comprobante viaja DENTRO de ``ReservaOut`` (INC-4) y las evidencias
    aparte, en su propio endpoint (INC-6, relación lazy a propósito para no
    pagar una consulta por página de listado). Esta prueba fija que desde el
    detalle se llega a los dos.
    """
    fecha = proximo_lunes(dias_minimos=2)
    reserva = _reservar(api_cliente, servicio_medio, vehiculo_id, fecha, 10)

    entrada = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": True}
    )
    assert entrada.status_code == 200, entrada.text
    foto = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/evidencias",
        json={
            "momento": "antes",
            "contenido_base64": PIXEL_PNG_BASE64,
            "mime": MIME_PNG,
            "observacion": "Rayón en el paragolpes",
        },
    )
    assert foto.status_code == 201, foto.text

    api_recepcion.post(f"{RUTA}/reservas/{reserva['id']}/asignacion", json={})
    for estado in ("en_lavado", "secado", "acabado", "finalizado"):
        api_operario.post(f"{RUTA}/reservas/{reserva['id']}/estado", json={"estado": estado})
    cobro = pagar_en_caja(api_recepcion, reserva["id"], reserva["monto"]["monto_centimos"])
    assert cobro.status_code == 201, cobro.text

    detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}")
    assert detalle.status_code == 200, detalle.text
    cuerpo = detalle.json()
    assert cuerpo["comprobante"]["archivo_url"].endswith("/archivo")

    evidencias = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}/evidencias")
    assert evidencias.status_code == 200, evidencias.text
    assert len(evidencias.json()["items"]) == 1


# --------------------------------------------------------------------------
# Lo que ya funcionaba sigue funcionando
# --------------------------------------------------------------------------
def test_ca03_los_filtros_no_cruzan_la_frontera_entre_clientes(
    api_cliente, api_admin, api_recepcion, servicio_corto, vehiculo_id
):
    """RF-017 `CA-03`: el filtro horizontal se aplica primero, siempre."""
    fecha = proximo_lunes(dias_minimos=2)
    mia = _reservar(api_cliente, servicio_corto, vehiculo_id, fecha, 10)

    ajeno = crear_vehiculo(api_admin, "ADM-222")
    de_otro = crear_reserva(api_admin, servicio_corto.id, ajeno, instante(fecha, 12))
    assert de_otro.status_code == 201, de_otro.text

    del_cliente = _listar(api_cliente, estado="confirmada")
    assert _codigos(del_cliente) == {mia["codigo"]}

    # La recepción tiene ``reserva:leer_todas``: ve las dos.
    assert len(_codigos(_listar(api_recepcion, estado="confirmada"))) == 2


def test_ca01_la_pagina_sigue_siendo_de_veinte(api_cliente):
    """`CA-01` no cambió: los filtros nuevos se aplican antes de paginar."""
    respuesta = _listar(api_cliente)

    assert respuesta.status_code == 200
    assert respuesta.json()["tamanio"] == 20
