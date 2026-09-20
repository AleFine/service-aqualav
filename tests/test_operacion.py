"""Counter operation: search, check-in, state machine and check-out.

RF-019, RF-021, RF-022 and RF-024.
"""

from datetime import timedelta

from sqlalchemy import func, select

from app.core.horario import a_utc, ahora
from app.models import Pago, Reserva, TransicionEstado
from tests.conftest import RUTA, codigo_error, crear_reserva, instante, proximo_lunes


def _reserva_confirmada(api_cliente, servicio, vehiculo_id, hora: int = 10) -> dict:
    respuesta = crear_reserva(
        api_cliente, servicio.id, vehiculo_id, instante(proximo_lunes(), hora, 0)
    )
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _check_in(api_personal, reserva_id: int):
    return api_personal.post(
        f"{RUTA}/reservas/{reserva_id}/check-in",
        json={"observaciones": "Rayón leve en la puerta", "confirmar_retraso": False},
    )


def _finalizar(api_personal, reserva_id: int):
    return api_personal.post(f"{RUTA}/reservas/{reserva_id}/estado", json={"estado": "finalizado"})


def _pagar(api_personal, reserva_id: int, monto: int, clave: str = "clave-1"):
    return api_personal.post(
        f"{RUTA}/reservas/{reserva_id}/pagos",
        json={"medio": "efectivo", "monto_centimos": monto},
        headers={"Idempotency-Key": clave},
    )


# --------------------------------------------------------------------------
# RF-019 - check-in
# --------------------------------------------------------------------------
def test_buscar_por_codigo_encuentra_la_reserva(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """RF-019 paso 1."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    respuesta = api_personal.get(f"{RUTA}/reservas/buscar", params={"codigo": reserva["codigo"]})

    assert respuesta.status_code == 200, respuesta.text
    items = respuesta.json()["items"]
    assert [item["id"] for item in items] == [reserva["id"]]
    # El personal ve los datos del cliente, el vehículo y el servicio (paso 2).
    assert items[0]["cliente"]["nombres"]
    assert items[0]["vehiculo"]["placa"] == "ABC-123"
    assert items[0]["servicio"]["duracion_min"] == 45


def test_buscar_por_placa_encuentra_la_reserva(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    respuesta = api_personal.get(f"{RUTA}/reservas/buscar", params={"placa": "abc-123"})

    assert respuesta.status_code == 200, respuesta.text
    assert len(respuesta.json()["items"]) == 1


def test_buscar_sin_coincidencias_responde_404(api_personal):
    respuesta = api_personal.get(f"{RUTA}/reservas/buscar", params={"codigo": "AQL-ZZZZZZ"})

    assert respuesta.status_code == 404


def test_el_check_in_pasa_a_en_atencion_y_registra_la_hora(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """RF-019 CA-01 y CA-03 (la bahía queda ocupada)."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    respuesta = _check_in(api_personal, reserva["id"])

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "en_atencion"
    assert cuerpo["hora_ingreso"] is not None
    assert cuerpo["bahia"]["id"] == reserva["bahia"]["id"]
    assert [fila["estado"] for fila in cuerpo["historial"]] == ["confirmada", "en_atencion"]


def test_el_check_in_de_una_reserva_ya_atendida_responde_422(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """RF-019 CA-02."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    assert _check_in(api_personal, reserva["id"]).status_code == 200

    respuesta = _check_in(api_personal, reserva["id"])

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"


def test_un_retraso_mayor_a_veinte_minutos_pide_confirmacion(
    api_personal, db, usuario_cliente, servicio_medio, vehiculo_id
):
    """RF-019 flujo 3a.

    La reserva se inserta directamente porque la API, por RN-02, nunca dejaría
    crear una que ya empezó.
    """
    inicio = ahora() - timedelta(minutes=45)
    reserva = Reserva(
        codigo="AQL-TARDES",
        usuario_id=usuario_cliente.id,
        vehiculo_id=vehiculo_id,
        servicio_id=servicio_medio.id,
        bahia_id=1,
        inicio=a_utc(inicio),
        fin=a_utc(inicio + timedelta(minutes=45)),
        estado="confirmada",
        monto_centimos=2500,
        moneda="PEN",
        modalidad_pago="presencial",
    )
    db.add(reserva)
    db.commit()

    sin_confirmar = api_personal.post(
        f"{RUTA}/reservas/{reserva.id}/check-in", json={"confirmar_retraso": False}
    )
    assert sin_confirmar.status_code == 409
    assert codigo_error(sin_confirmar) == "RETRASO_REQUIERE_CONFIRMACION"

    confirmado = api_personal.post(
        f"{RUTA}/reservas/{reserva.id}/check-in", json={"confirmar_retraso": True}
    )
    assert confirmado.status_code == 200, confirmado.text
    assert confirmado.json()["estado"] == "en_atencion"


# --------------------------------------------------------------------------
# RF-021 - state machine
# --------------------------------------------------------------------------
def test_volver_de_en_atencion_a_confirmada_responde_422(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """RF-021 CA-01: esa transición no está declarada."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_personal, reserva["id"])

    respuesta = api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/estado", json={"estado": "confirmada"}
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"
    # El error indica la secuencia válida (flujo 4a).
    assert respuesta.json()["error"]["detalles"]


def test_cada_transicion_escribe_una_fila_de_historial(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """RF-021 CA-02 / EXTENSION POINT P7."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_personal, reserva["id"])

    respuesta = _finalizar(api_personal, reserva["id"])

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert [fila["estado"] for fila in cuerpo["historial"]] == [
        "confirmada",
        "en_atencion",
        "finalizado",
    ]
    for fila in cuerpo["historial"]:
        assert fila["ocurrido_en"]
        assert fila["autor"], "cada fila guarda quién la provocó"
    # RF-022 CA-02: el cliente ve la hora real de término.
    assert cuerpo["hora_fin_real"] is not None


def test_una_transicion_nueva_en_la_tabla_funciona_sin_tocar_codigo(
    api_cliente, api_personal, db, servicio_medio, vehiculo_id
):
    """RF-021 CA-03 / EXTENSION POINT P3.

    Insertar una fila en ``transicion_estado`` habilita el salto directo de
    ``confirmada`` a ``finalizado``. No se despliega nada.
    """
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    antes = _finalizar(api_personal, reserva["id"])
    assert antes.status_code == 422

    db.add(
        TransicionEstado(
            estado_origen="confirmada",
            estado_destino="finalizado",
            permiso_requerido="reserva:avanzar_estado",
        )
    )
    db.commit()

    despues = _finalizar(api_personal, reserva["id"])

    assert despues.status_code == 200, despues.text
    assert despues.json()["estado"] == "finalizado"


def test_las_transiciones_permitidas_dependen_de_los_permisos(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """La app dibuja sus botones con esta lista, nunca con condicionales locales."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    del_cliente = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
    del_personal = api_personal.get(f"{RUTA}/reservas/{reserva['id']}").json()

    assert del_cliente["transiciones_permitidas"] == ["cancelada"]
    assert set(del_personal["transiciones_permitidas"]) == {"en_atencion", "cancelada"}


# --------------------------------------------------------------------------
# RF-024 - check-out
# --------------------------------------------------------------------------
def test_no_se_entrega_sin_pago_confirmado(api_cliente, api_personal, servicio_medio, vehiculo_id):
    """RF-024 CA-01 / RN-09."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_personal, reserva["id"])
    _finalizar(api_personal, reserva["id"])

    respuesta = api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "PAGO_PENDIENTE"


def test_no_se_entrega_un_servicio_que_no_esta_finalizado(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_personal, reserva["id"])

    respuesta = api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"


def test_con_el_pago_confirmado_la_entrega_libera_la_bahia(
    api_cliente, api_personal, db, servicio_corto, vehiculo_id
):
    """RF-024 CA-02: tras entregar, el bloque vuelve a ofrecerse."""
    from tests.conftest import dejar_una_sola_bahia

    dejar_una_sola_bahia(db)
    fecha = proximo_lunes()
    reserva = crear_reserva(
        api_cliente, servicio_corto.id, vehiculo_id, instante(fecha, 10, 0)
    ).json()

    _check_in(api_personal, reserva["id"])
    _finalizar(api_personal, reserva["id"])
    assert _pagar(api_personal, reserva["id"], 1500).status_code == 201

    respuesta = api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "entregado"
    assert cuerpo["hora_entrega"] is not None
    assert cuerpo["transiciones_permitidas"] == [], "estado terminal"

    disponibilidad = api_cliente.get(
        f"{RUTA}/disponibilidad",
        params={"fecha": fecha.isoformat(), "servicio_id": servicio_corto.id},
    ).json()
    from datetime import datetime as _dt

    horas = {_dt.fromisoformat(b["inicio"]).strftime("%H:%M") for b in disponibilidad["bloques"]}
    assert "10:00" in horas


# --------------------------------------------------------------------------
# C1 - alternate routes into a state that has its own operation
#
# ``transicion_estado.endpoint`` declares which operation OWNS a move. The
# generic ``POST /estado`` carries no invariant of its own, so it must refuse
# every owned move: reaching ``entregado`` through it used to hand the vehicle
# back with no payment at all (RN-09, RF-024 CA-01), and reaching ``cancelada``
# left the reason, the author and the date empty (RF-016 CA-03).
# --------------------------------------------------------------------------
def _estado_generico(api_personal, reserva_id: int, estado: str):
    return api_personal.post(f"{RUTA}/reservas/{reserva_id}/estado", json={"estado": estado})


def test_estado_generico_no_entrega_saltandose_checkout(
    api_cliente, api_personal, db, servicio_medio, vehiculo_id
):
    """RN-09 / RF-024 CA-01: sin pago no se entrega, por ninguna puerta."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_personal, reserva["id"])
    _finalizar(api_personal, reserva["id"])

    respuesta = _estado_generico(api_personal, reserva["id"], "entregado")

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"

    fila = db.get(Reserva, reserva["id"])
    db.refresh(fila)
    assert fila.estado == "finalizado", "la reserva no se movió"
    assert fila.hora_entrega is None
    assert fila.conformidad_cliente is None
    assert db.scalar(select(func.count(Pago.id)).where(Pago.reserva_id == reserva["id"])) == 0


def test_estado_generico_no_cancela_saltandose_cancelacion(
    api_cliente, api_personal, db, servicio_medio, vehiculo_id
):
    """RF-016 CA-03: cancelar exige motivo, autor y fecha."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    respuesta = _estado_generico(api_personal, reserva["id"], "cancelada")

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"

    fila = db.get(Reserva, reserva["id"])
    db.refresh(fila)
    assert fila.estado == "confirmada"
    assert fila.motivo_cancelacion is None
    assert fila.cancelada_por_id is None
    assert fila.cancelada_en is None

    # La puerta legítima sigue abierta y sí deja rastro.
    legitima = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/cancelacion", json={"motivo": "cambio de planes"}
    )
    assert legitima.status_code == 200, legitima.text
    assert legitima.json()["cancelacion"]["motivo"] == "cambio de planes"
    assert legitima.json()["cancelacion"]["autor"]


def test_checkout_sigue_funcionando_con_pago_confirmado(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """C1 cierra las rutas alternas sin tocar el camino feliz (RF-024 CA-02)."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_personal, reserva["id"])
    _finalizar(api_personal, reserva["id"])
    assert _pagar(api_personal, reserva["id"], 2500).status_code == 201

    respuesta = api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "entregado"
    assert cuerpo["hora_entrega"] is not None
    assert cuerpo["conformidad_cliente"] is True


def test_transicion_sin_endpoint_sigue_disponible_en_estado(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """Una transición sin dueño declarado la sigue haciendo ``POST /estado``."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_personal, reserva["id"])

    respuesta = _estado_generico(api_personal, reserva["id"], "finalizado")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "finalizado"


def test_el_checkin_de_un_estado_nuevo_solo_lo_declara_la_tabla(
    api_cliente, api_personal, db, servicio_medio, vehiculo_id
):
    """C1 quitó el literal ``confirmada`` del check-in: el origen es dato.

    Se declara ``finalizado -> en_atencion`` con dueño ``check_in`` y el
    check-in de una reserva finalizada pasa a ser válido, sin tocar código.
    """
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_personal, reserva["id"])
    _finalizar(api_personal, reserva["id"])

    assert _check_in(api_personal, reserva["id"]).status_code == 422

    db.add(
        TransicionEstado(
            estado_origen="finalizado",
            estado_destino="en_atencion",
            permiso_requerido="reserva:check_in",
            endpoint="check_in",
            marca_fin_servicio=False,
        )
    )
    db.commit()

    despues = _check_in(api_personal, reserva["id"])

    assert despues.status_code == 200, despues.text
    assert despues.json()["estado"] == "en_atencion"


# --------------------------------------------------------------------------
# C3 - a state added as data is a working state
# --------------------------------------------------------------------------
def _declarar_en_lavado(db) -> None:
    """``en_atencion -> en_lavado -> finalizado``, insertado como dato."""
    db.add(
        TransicionEstado(
            estado_origen="en_atencion",
            estado_destino="en_lavado",
            permiso_requerido="reserva:avanzar_estado",
            endpoint=None,
            marca_fin_servicio=False,
        )
    )
    db.add(
        TransicionEstado(
            estado_origen="en_lavado",
            estado_destino="finalizado",
            permiso_requerido="reserva:avanzar_estado",
            endpoint=None,
            marca_fin_servicio=True,
        )
    )
    db.commit()


def test_buscar_encuentra_reserva_en_estado_nuevo(
    api_cliente, api_personal, db, servicio_medio, vehiculo_id
):
    """RF-019: el personal encuentra un servicio en curso, esté en el estado
    que esté. Antes de C3 un estado nuevo era invisible en la búsqueda."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_personal, reserva["id"])
    _declarar_en_lavado(db)

    assert _estado_generico(api_personal, reserva["id"], "en_lavado").status_code == 200

    respuesta = api_personal.get(f"{RUTA}/reservas/buscar", params={"codigo": reserva["codigo"]})

    assert respuesta.status_code == 200, respuesta.text
    assert [item["id"] for item in respuesta.json()["items"]] == [reserva["id"]]


# --------------------------------------------------------------------------
# C4 - state catalogue
# --------------------------------------------------------------------------
def test_catalogo_de_estados_incluye_estado_insertado_como_dato(api_personal, db):
    """P3: la pantalla agregada del móvil se dibuja desde este catálogo."""
    antes = {item["codigo"] for item in api_personal.get(f"{RUTA}/estados").json()["items"]}
    assert "en_lavado" not in antes

    _declarar_en_lavado(db)

    respuesta = api_personal.get(f"{RUTA}/estados")

    assert respuesta.status_code == 200, respuesta.text
    items = {item["codigo"]: item for item in respuesta.json()["items"]}
    assert "en_lavado" in items
    assert items["en_lavado"]["terminal"] is False
    # Queda entre en_atencion y finalizado, que es por donde se declaró.
    assert items["en_atencion"]["orden"] < items["en_lavado"]["orden"]
    assert items["en_lavado"]["orden"] < items["finalizado"]["orden"]


def test_catalogo_marca_terminales_correctamente(api_personal):
    """Terminal = ninguna transición sale de ese estado. No hay lista en código."""
    items = {item["codigo"]: item for item in api_personal.get(f"{RUTA}/estados").json()["items"]}

    assert items["entregado"]["terminal"] is True
    assert items["cancelada"]["terminal"] is True
    assert items["confirmada"]["terminal"] is False
    assert items["en_atencion"]["terminal"] is False
    assert items["finalizado"]["terminal"] is False, "RF-024 CA-02: aún falta entregar"


def test_el_catalogo_de_estados_requiere_token(cliente_http):
    respuesta = cliente_http.get(f"{RUTA}/estados")

    assert respuesta.status_code == 401


# --------------------------------------------------------------------------
# C9 - what check-in and check-out recorded is readable again
# --------------------------------------------------------------------------
def test_las_observaciones_del_ingreso_solo_las_ve_el_personal(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """Nota interna del taller: la ve quien tiene ``reserva:leer_todas``."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_personal, reserva["id"])

    del_personal = api_personal.get(f"{RUTA}/reservas/{reserva['id']}").json()
    del_cliente = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()

    assert del_personal["observaciones_ingreso"] == "Rayón leve en la puerta"
    assert del_cliente["observaciones_ingreso"] is None
    # La conformidad es la respuesta del propio cliente: la ve todo el mundo.
    assert "conformidad_cliente" in del_cliente
