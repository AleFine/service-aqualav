"""Counter and bay operation: search, check-in, state machine and check-out.

RF-019, RF-021, RF-022 and RF-024, on the eleven states of Annex A v1.0.

The chain a service walks is now ``confirmada -> en_recepcion -> asignado ->
en_lavado -> secado -> acabado -> finalizado -> entregado``, and it is split
between two roles: the receptionist owns the doors of the shop (check-in,
assignment, charge, delivery) and the operator owns the bay (RF-021).
"""

import ast
import pathlib
from datetime import timedelta

from sqlalchemy import func, select

from app.core.horario import a_utc, ahora
from app.models import EstadoReserva, Pago, Reserva, TransicionEstado
from tests.conftest import (
    RUTA,
    asignar,
    avanzar_estado,
    codigo_error,
    crear_reserva,
    instante,
    llevar_hasta_finalizado,
    proximo_lunes,
)

RAIZ_APP = pathlib.Path(__file__).resolve().parent.parent / "app"


def _reserva_confirmada(api_cliente, servicio, vehiculo_id, hora: int = 10) -> dict:
    respuesta = crear_reserva(
        api_cliente, servicio.id, vehiculo_id, instante(proximo_lunes(), hora, 0)
    )
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


def _check_in(api_recepcion, reserva_id: int):
    return api_recepcion.post(
        f"{RUTA}/reservas/{reserva_id}/check-in",
        json={"observaciones": "Rayón leve en la puerta", "confirmar_retraso": False},
    )


def _pagar(api_recepcion, reserva_id: int, monto: int, clave: str = "clave-1"):
    return api_recepcion.post(
        f"{RUTA}/reservas/{reserva_id}/pagos",
        json={"medio": "efectivo", "monto_centimos": monto},
        headers={"Idempotency-Key": clave},
    )


# --------------------------------------------------------------------------
# RF-019 - check-in
# --------------------------------------------------------------------------
def test_buscar_por_codigo_encuentra_la_reserva(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id
):
    """RF-019 paso 1."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    respuesta = api_recepcion.get(f"{RUTA}/reservas/buscar", params={"codigo": reserva["codigo"]})

    assert respuesta.status_code == 200, respuesta.text
    items = respuesta.json()["items"]
    assert [item["id"] for item in items] == [reserva["id"]]
    # El personal ve los datos del cliente, el vehículo y el servicio (paso 2).
    assert items[0]["cliente"]["nombres"]
    assert items[0]["vehiculo"]["placa"] == "ABC-123"
    assert items[0]["servicio"]["duracion_min"] == 45


def test_buscar_por_placa_encuentra_la_reserva(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id
):
    _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    respuesta = api_recepcion.get(f"{RUTA}/reservas/buscar", params={"placa": "abc-123"})

    assert respuesta.status_code == 200, respuesta.text
    assert len(respuesta.json()["items"]) == 1


def test_buscar_sin_coincidencias_responde_404(api_recepcion):
    respuesta = api_recepcion.get(f"{RUTA}/reservas/buscar", params={"codigo": "AQL-ZZZZZZ"})

    assert respuesta.status_code == 404


def test_el_check_in_pasa_a_en_recepcion_y_registra_la_hora(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id
):
    """RF-019 CA-01 en su redacción v1.0: el destino es «En recepción».

    El MVP aterrizaba en ``en_atencion``, que el Anexo A v1.0 elimina. Ninguna
    línea de ``operacion_service`` cambió: el destino lo declara la fila.
    """
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    respuesta = _check_in(api_recepcion, reserva["id"])

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "en_recepcion"
    assert cuerpo["hora_ingreso"] is not None
    assert cuerpo["bahia"]["id"] == reserva["bahia"]["id"]
    assert [fila["estado"] for fila in cuerpo["historial"]] == ["confirmada", "en_recepcion"]


def test_el_check_in_de_una_reserva_ya_atendida_responde_422(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id
):
    """RF-019 CA-02."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    assert _check_in(api_recepcion, reserva["id"]).status_code == 200

    respuesta = _check_in(api_recepcion, reserva["id"])

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"


def test_un_retraso_mayor_a_veinte_minutos_pide_confirmacion(
    api_recepcion, db, usuario_cliente, servicio_medio, vehiculo_id
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

    sin_confirmar = api_recepcion.post(
        f"{RUTA}/reservas/{reserva.id}/check-in", json={"confirmar_retraso": False}
    )
    assert sin_confirmar.status_code == 409
    assert codigo_error(sin_confirmar) == "RETRASO_REQUIERE_CONFIRMACION"

    confirmado = api_recepcion.post(
        f"{RUTA}/reservas/{reserva.id}/check-in", json={"confirmar_retraso": True}
    )
    assert confirmado.status_code == 200, confirmado.text
    assert confirmado.json()["estado"] == "en_recepcion"


# --------------------------------------------------------------------------
# RF-021 - state machine (Anexo A v1.0)
# --------------------------------------------------------------------------
def test_volver_de_en_lavado_a_en_recepcion_responde_422(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-021 CA-01 en su redacción v1.0: «En lavado» no vuelve a «En recepción»."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_recepcion, reserva["id"])
    assert asignar(api_recepcion, reserva["id"]).status_code == 200
    assert avanzar_estado(api_operario, reserva["id"], "en_lavado").status_code == 200

    respuesta = avanzar_estado(api_operario, reserva["id"], "en_recepcion")

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"
    # El error indica la secuencia válida (flujo 4a).
    assert respuesta.json()["error"]["detalles"]


def test_la_cadena_de_bahia_la_recorre_el_operario(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """Anexo A v1.0: ``asignado -> en_lavado -> secado -> acabado -> finalizado``."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_recepcion, reserva["id"])
    assert asignar(api_recepcion, reserva["id"]).status_code == 200

    recorrido = []
    for estado in ("en_lavado", "secado", "acabado", "finalizado"):
        respuesta = avanzar_estado(api_operario, reserva["id"], estado)
        assert respuesta.status_code == 200, respuesta.text
        recorrido.append(respuesta.json()["estado"])

    assert recorrido == ["en_lavado", "secado", "acabado", "finalizado"]
    # RF-022 CA-02: ``acabado -> finalizado`` es la fila que marca el fin.
    assert respuesta.json()["hora_fin_real"] is not None


def test_el_recepcionista_no_avanza_el_estado_del_servicio(
    api_cliente, api_recepcion, db, servicio_medio, vehiculo_id
):
    """RF-004 v1.0: el mostrador y la bahía son dos roles distintos."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_recepcion, reserva["id"])
    assert asignar(api_recepcion, reserva["id"]).status_code == 200

    respuesta = avanzar_estado(api_recepcion, reserva["id"], "en_lavado")

    assert respuesta.status_code == 403
    assert codigo_error(respuesta) == "PERMISO_DENEGADO"


def test_cada_transicion_escribe_una_fila_de_historial(
    api_cliente, api_recepcion, api_operario, db, usuario_operario, servicio_medio, vehiculo_id
):
    """RF-021 CA-02 / EXTENSION POINT P7."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])

    cuerpo = api_operario.get(f"{RUTA}/reservas/{reserva['id']}").json()
    assert [fila["estado"] for fila in cuerpo["historial"]] == [
        "confirmada",
        "en_recepcion",
        "asignado",
        "en_lavado",
        "secado",
        "acabado",
        "finalizado",
    ]
    for fila in cuerpo["historial"]:
        assert fila["ocurrido_en"]
        assert fila["autor"], "cada fila guarda quién la provocó"
    # RF-022 CA-02: el cliente ve la hora real de término.
    assert cuerpo["hora_fin_real"] is not None


def test_una_transicion_nueva_en_la_tabla_funciona_sin_tocar_codigo(
    api_cliente, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-021 CA-03 / EXTENSION POINT P3.

    Insertar una fila en ``transicion_estado`` habilita el salto directo de
    ``confirmada`` a ``finalizado``. No se despliega nada.
    """
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    antes = avanzar_estado(api_operario, reserva["id"], "finalizado")
    assert antes.status_code == 422

    db.add(
        TransicionEstado(
            estado_origen="confirmada",
            estado_destino="finalizado",
            permiso_requerido="reserva:avanzar_estado",
        )
    )
    db.commit()

    despues = avanzar_estado(api_operario, reserva["id"], "finalizado")

    assert despues.status_code == 200, despues.text
    assert despues.json()["estado"] == "finalizado"


def test_las_transiciones_permitidas_dependen_de_los_permisos(
    api_cliente, api_recepcion, api_operario, servicio_medio, vehiculo_id
):
    """La app dibuja sus botones con esta lista, nunca con condicionales locales."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    del_cliente = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
    del_recepcion = api_recepcion.get(f"{RUTA}/reservas/{reserva['id']}").json()
    del_operario = api_operario.get(f"{RUTA}/reservas/{reserva['id']}").json()

    assert del_cliente["transiciones_permitidas"] == ["cancelada"]
    assert set(del_recepcion["transiciones_permitidas"]) == {"en_recepcion", "cancelada"}
    # El operario todavía no tiene nada que hacer con una reserva confirmada.
    assert del_operario["transiciones_permitidas"] == []


# --------------------------------------------------------------------------
# RF-024 - check-out
# --------------------------------------------------------------------------
def test_no_se_entrega_sin_pago_confirmado(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-024 CA-01 / RN-09."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])

    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "PAGO_PENDIENTE"


def test_no_se_entrega_un_servicio_que_no_esta_finalizado(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id
):
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_recepcion, reserva["id"])

    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"


def test_con_el_pago_confirmado_la_entrega_libera_la_bahia(
    api_cliente, api_recepcion, api_operario, db, servicio_corto, vehiculo_id
):
    """RF-024 CA-02: tras entregar, el bloque vuelve a ofrecerse."""
    from tests.conftest import dejar_una_sola_bahia

    dejar_una_sola_bahia(db)
    fecha = proximo_lunes()
    reserva = crear_reserva(
        api_cliente, servicio_corto.id, vehiculo_id, instante(fecha, 10, 0)
    ).json()

    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])
    assert _pagar(api_recepcion, reserva["id"], 1500).status_code == 201

    respuesta = api_recepcion.post(
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
#
# El administrador es quien ejerce estas pruebas: tiene TODOS los permisos, así
# que si la puerta se cierra no es por falta de permiso sino por el dueño de la
# transición, que es justo lo que se quiere verificar.
# --------------------------------------------------------------------------
def test_estado_generico_no_entrega_saltandose_checkout(
    api_cliente, api_recepcion, api_operario, api_admin, db, servicio_medio, vehiculo_id
):
    """RN-09 / RF-024 CA-01: sin pago no se entrega, por ninguna puerta."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])

    respuesta = avanzar_estado(api_admin, reserva["id"], "entregado")

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"

    fila = db.get(Reserva, reserva["id"])
    db.refresh(fila)
    assert fila.estado == "finalizado", "la reserva no se movió"
    assert fila.hora_entrega is None
    assert fila.conformidad_cliente is None
    assert db.scalar(select(func.count(Pago.id)).where(Pago.reserva_id == reserva["id"])) == 0


def test_estado_generico_no_cancela_saltandose_cancelacion(
    api_cliente, api_admin, db, servicio_medio, vehiculo_id
):
    """RF-016 CA-03: cancelar exige motivo, autor y fecha."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    respuesta = avanzar_estado(api_admin, reserva["id"], "cancelada")

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


def test_estado_generico_no_asigna_saltandose_la_asignacion(
    api_cliente, api_recepcion, api_admin, servicio_medio, vehiculo_id
):
    """RF-020: ``en_recepcion -> asignado`` tiene su propia operación.

    INC-1B la implementa (``POST /reservas/{id}/asignacion``). La fila ya
    existe, así que el endpoint genérico la rechaza desde hoy y nadie puede
    marcar un servicio como asignado sin ocupar la bahía ni encolar al operario.
    """
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_recepcion, reserva["id"])

    respuesta = avanzar_estado(api_admin, reserva["id"], "asignado")

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"
    assert "asignacion" in str(respuesta.json()["error"]["detalles"])


def test_checkout_sigue_funcionando_con_pago_confirmado(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """C1 cierra las rutas alternas sin tocar el camino feliz (RF-024 CA-02)."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])
    assert _pagar(api_recepcion, reserva["id"], 2500).status_code == 201

    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "entregado"
    assert cuerpo["hora_entrega"] is not None
    assert cuerpo["conformidad_cliente"] is True


def test_transicion_sin_endpoint_sigue_disponible_en_estado(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """Una transición sin dueño declarado la sigue haciendo ``POST /estado``."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_recepcion, reserva["id"])
    assert asignar(api_recepcion, reserva["id"]).status_code == 200

    respuesta = avanzar_estado(api_operario, reserva["id"], "en_lavado")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "en_lavado"


def test_el_checkin_aterriza_donde_diga_la_tabla(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """P3 completo: el check-in no fija ni el origen NI el destino.

    Se declara ``finalizado -> recepcion_express``, un estado que no existe en
    ningún enum ni en ninguna línea de código, con dueño ``check_in``. El
    check-in de una reserva finalizada pasa a ser válido y aterriza ahí, sin
    desplegar nada. Antes de INC-1A el destino era el literal ``en_atencion``.
    """
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])

    assert _check_in(api_recepcion, reserva["id"]).status_code == 422

    db.add(
        TransicionEstado(
            estado_origen="finalizado",
            estado_destino="recepcion_express",
            permiso_requerido="reserva:check_in",
            endpoint="check_in",
            marca_fin_servicio=False,
        )
    )
    db.commit()

    despues = _check_in(api_recepcion, reserva["id"])

    assert despues.status_code == 200, despues.text
    assert despues.json()["estado"] == "recepcion_express"


# --------------------------------------------------------------------------
# C3 - a state added as data is a working state
# --------------------------------------------------------------------------
def _declarar_encerado(db) -> None:
    """``acabado -> encerado -> finalizado``, insertado como dato."""
    db.add(
        TransicionEstado(
            estado_origen="acabado",
            estado_destino="encerado",
            permiso_requerido="reserva:avanzar_estado",
            endpoint=None,
            marca_fin_servicio=False,
        )
    )
    db.add(
        TransicionEstado(
            estado_origen="encerado",
            estado_destino="finalizado",
            permiso_requerido="reserva:avanzar_estado",
            endpoint=None,
            marca_fin_servicio=True,
        )
    )
    db.commit()


def test_buscar_encuentra_reserva_en_estado_nuevo(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-019: el personal encuentra un servicio en curso, esté en el estado
    que esté. Antes de C3 un estado nuevo era invisible en la búsqueda."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_recepcion, reserva["id"])
    assert asignar(api_recepcion, reserva["id"]).status_code == 200
    for estado in ("en_lavado", "secado", "acabado"):
        assert avanzar_estado(api_operario, reserva["id"], estado).status_code == 200
    _declarar_encerado(db)

    assert avanzar_estado(api_operario, reserva["id"], "encerado").status_code == 200

    respuesta = api_recepcion.get(f"{RUTA}/reservas/buscar", params={"codigo": reserva["codigo"]})

    assert respuesta.status_code == 200, respuesta.text
    assert [item["id"] for item in respuesta.json()["items"]] == [reserva["id"]]


# --------------------------------------------------------------------------
# C4 - state catalogue (Anexo A v1.0)
# --------------------------------------------------------------------------
def test_el_catalogo_expone_los_once_estados_del_anexo_a(api_recepcion):
    """Anexo A v1.0: once estados, y ``en_atencion`` ya no está."""
    items = {item["codigo"] for item in api_recepcion.get(f"{RUTA}/estados").json()["items"]}

    assert items == {estado.value for estado in EstadoReserva}
    assert len(items) == 11
    assert "en_atencion" not in items


def test_la_tabla_declara_las_transiciones_del_anexo_a(db):
    """Las catorce filas del Anexo A v1.0, con su dueño y su marca de fin.

    Las otras cuatro transiciones del anexo no son filas: dos son la creación
    de la reserva (no hay estado de origen) y dos son los sumideros terminales,
    que se derivan de la ausencia de filas salientes.
    """
    filas = db.scalars(select(TransicionEstado)).all()
    declaradas = {(fila.estado_origen, fila.estado_destino): fila for fila in filas}

    assert len(filas) == 14
    assert declaradas[("confirmada", "en_recepcion")].endpoint == "check_in"
    assert declaradas[("en_recepcion", "asignado")].endpoint == "asignacion"
    assert declaradas[("en_recepcion", "asignado")].permiso_requerido == "reserva:asignar"
    assert declaradas[("finalizado", "en_revision")].permiso_requerido == "reserva:revisar"
    assert declaradas[("en_revision", "entregado")].endpoint == "check_out"

    # RF-022 CA-02: el fin del servicio lo marca ``acabado -> finalizado``, y
    # ninguna otra fila.
    con_fin = {
        (fila.estado_origen, fila.estado_destino) for fila in filas if fila.marca_fin_servicio
    }
    assert con_fin == {("acabado", "finalizado")}


def test_catalogo_de_estados_incluye_estado_insertado_como_dato(api_recepcion, db):
    """P3: la pantalla agregada del móvil se dibuja desde este catálogo."""
    antes = {item["codigo"] for item in api_recepcion.get(f"{RUTA}/estados").json()["items"]}
    assert "encerado" not in antes

    _declarar_encerado(db)

    respuesta = api_recepcion.get(f"{RUTA}/estados")

    assert respuesta.status_code == 200, respuesta.text
    items = {item["codigo"]: item for item in respuesta.json()["items"]}
    assert "encerado" in items
    assert items["encerado"]["terminal"] is False
    # Queda entre acabado y finalizado, que es por donde se declaró.
    assert items["acabado"]["orden"] < items["encerado"]["orden"]
    assert items["encerado"]["orden"] < items["finalizado"]["orden"]


def test_catalogo_marca_terminales_correctamente(api_recepcion):
    """Terminal = ninguna transición sale de ese estado. No hay lista en código."""
    items = {item["codigo"]: item for item in api_recepcion.get(f"{RUTA}/estados").json()["items"]}

    assert items["entregado"]["terminal"] is True
    assert items["cancelada"]["terminal"] is True
    assert items["confirmada"]["terminal"] is False
    assert items["en_lavado"]["terminal"] is False
    assert items["finalizado"]["terminal"] is False, "RF-024 CA-02: aún falta entregar"


def test_el_catalogo_de_estados_requiere_token(cliente_http):
    respuesta = cliente_http.get(f"{RUTA}/estados")

    assert respuesta.status_code == 401


# --------------------------------------------------------------------------
# P3 by code inspection, the twin of RF-004 CA-03 for the state machine
# --------------------------------------------------------------------------
def _estados_en_comparaciones(arbol: ast.AST) -> list[str]:
    """State names used inside any comparison of a module."""
    nombres = {estado.value for estado in EstadoReserva}
    encontradas: list[str] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Compare):
            continue
        for operando in [nodo.left, *nodo.comparators]:
            for hijo in ast.walk(operando):
                if (
                    isinstance(hijo, ast.Constant)
                    and isinstance(hijo.value, str)
                    and hijo.value in nombres
                ):
                    encontradas.append(hijo.value)
    return encontradas


def test_ningun_modulo_compara_el_nombre_de_un_estado():
    """EXTENSION POINT P3: la máquina de estados vive en la tabla.

    El gemelo de ``RF-004 CA-03`` para los estados: si un servicio comparase
    con un estado concreto, añadir un estado dejaría de ser insertar una fila.
    ``app/seed.py`` (que siembra la tabla) y ``app/models/enums.py`` (que solo
    deletrea el vocabulario) son las dos excepciones.
    """
    permitidos = {RAIZ_APP / "seed.py", RAIZ_APP / "models" / "enums.py"}
    revisados = 0
    infracciones: list[str] = []

    for archivo in sorted(RAIZ_APP.rglob("*.py")):
        if archivo in permitidos:
            continue
        revisados += 1
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nombre in _estados_en_comparaciones(arbol):
            infracciones.append(f"{archivo.relative_to(RAIZ_APP)}: compara con «{nombre}»")

    assert revisados > 10, "el recorrido debería cubrir todo el paquete"
    assert infracciones == []


# --------------------------------------------------------------------------
# C9 - what check-in and check-out recorded is readable again
# --------------------------------------------------------------------------
def test_las_observaciones_del_ingreso_solo_las_ve_el_personal(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id
):
    """Nota interna del taller: la ve quien tiene ``reserva:leer_todas``."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    _check_in(api_recepcion, reserva["id"])

    del_recepcion = api_recepcion.get(f"{RUTA}/reservas/{reserva['id']}").json()
    del_cliente = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()

    assert del_recepcion["observaciones_ingreso"] == "Rayón leve en la puerta"
    assert del_cliente["observaciones_ingreso"] is None
    # La conformidad es la respuesta del propio cliente: la ve todo el mundo.
    assert "conformidad_cliente" in del_cliente


# --------------------------------------------------------------------------
# RF-019 delta v1.0 - the QR and the walk-in customer
# --------------------------------------------------------------------------
def test_la_reserva_nace_con_su_codigo_qr(api_cliente, api_recepcion, servicio_medio, vehiculo_id):
    """RF-019 v1.0: el ticket de recepción lleva un QR escaneable."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    del_personal = api_recepcion.get(f"{RUTA}/reservas/{reserva['id']}").json()

    assert del_personal["codigo_qr"].startswith("AQLQR-")
    assert del_personal["codigo_qr"] != del_personal["codigo"], "el QR no es el código hablado"
    # Es una credencial de escaneo: sigue la misma regla horizontal que las
    # observaciones de ingreso.
    assert api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()["codigo_qr"] is None


def test_buscar_por_qr_encuentra_la_reserva(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id
):
    """RF-019 delta: «búsqueda por escaneo de código QR»."""
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)
    qr = api_recepcion.get(f"{RUTA}/reservas/{reserva['id']}").json()["codigo_qr"]

    respuesta = api_recepcion.get(f"{RUTA}/reservas/buscar", params={"qr": qr})

    assert respuesta.status_code == 200, respuesta.text
    assert [item["id"] for item in respuesta.json()["items"]] == [reserva["id"]]


def test_buscar_con_un_qr_desconocido_responde_404(api_recepcion):
    respuesta = api_recepcion.get(f"{RUTA}/reservas/buscar", params={"qr": "AQLQR-ZZZZZZZZZZZZ"})

    assert respuesta.status_code == 404


def test_la_atencion_sin_reserva_entra_directamente_en_recepcion(
    api_recepcion, db, servicio_corto, vehiculo_id, monkeypatch
):
    """RF-019 flujo 1a: «el recepcionista crea una atención inmediata».

    El reloj se fija a un lunes a las 10:00 para que la prueba no dependa de la
    hora real a la que se ejecute la suite.
    """
    from app.services import reserva_service

    momento = instante(proximo_lunes(), 10, 0)
    monkeypatch.setattr(reserva_service, "ahora", lambda: momento)

    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/atencion-inmediata",
        json={
            "servicio_id": servicio_corto.id,
            "vehiculo_id": vehiculo_id,
            "observaciones": "Llegó sin cita, pide lavado rápido",
        },
    )

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "en_recepcion", "aterriza donde diga la tabla, como el check-in"
    assert cuerpo["atencion_sin_reserva"] is True
    assert cuerpo["hora_ingreso"] is not None
    assert cuerpo["observaciones_ingreso"] == "Llegó sin cita, pide lavado rápido"
    # La línea de tiempo se lee igual que la de una reserva agendada.
    assert [fila["estado"] for fila in cuerpo["historial"]] == ["confirmada", "en_recepcion"]


def test_la_atencion_sin_reserva_necesita_una_bahia_libre(
    api_cliente, api_recepcion, db, servicio_corto, vehiculo_id, monkeypatch
):
    """RF-019 flujo 1a: «si hay una bahía libre»."""
    from app.services import reserva_service
    from tests.conftest import dejar_una_sola_bahia

    dejar_una_sola_bahia(db)
    fecha = proximo_lunes()
    assert (
        crear_reserva(
            api_cliente, servicio_corto.id, vehiculo_id, instante(fecha, 10, 0)
        ).status_code
        == 201
    )
    monkeypatch.setattr(reserva_service, "ahora", lambda: instante(fecha, 10, 0))

    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/atencion-inmediata",
        json={"servicio_id": servicio_corto.id, "vehiculo_id": vehiculo_id},
    )

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "RESERVA_BLOQUE_OCUPADO"


def test_la_atencion_sin_reserva_exige_el_permiso_del_mostrador(
    api_cliente, api_operario, servicio_corto, vehiculo_id
):
    cuerpo = {"servicio_id": servicio_corto.id, "vehiculo_id": vehiculo_id}

    assert api_cliente.post(f"{RUTA}/reservas/atencion-inmediata", json=cuerpo).status_code == 403
    assert api_operario.post(f"{RUTA}/reservas/atencion-inmediata", json=cuerpo).status_code == 403


# --------------------------------------------------------------------------
# RF-024 delta v1.0 - the customer objects and the service goes back
# --------------------------------------------------------------------------
def _finalizada_y_pagada(api_cliente, api_recepcion, api_operario, db, servicio, vehiculo_id):
    reserva = _reserva_confirmada(api_cliente, servicio, vehiculo_id)
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])
    assert _pagar(api_recepcion, reserva["id"], 2500).status_code == 201
    return reserva


def test_la_observacion_del_cliente_manda_el_servicio_a_revision(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-024 flujo 3a: «el servicio pasa a "En revisión"»."""
    reserva = _finalizada_y_pagada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )

    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/revision",
        json={"observacion": "Quedaron restos de cera en el parabrisas"},
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "en_revision"
    assert cuerpo["observacion_revision"] == "Quedaron restos de cera en el parabrisas"
    assert cuerpo["conformidad_cliente"] is False
    assert cuerpo["historial"][-1]["estado"] == "en_revision"
    assert cuerpo["hora_entrega"] is None, "el vehículo no salió del local"


def test_desde_en_revision_el_operario_reprocesa(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """Anexo A v1.0: ``en_revision -> acabado`` lo hace el genérico (RF-021)."""
    reserva = _finalizada_y_pagada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/revision", json={"observacion": "Falta secar"}
    )

    respuesta = avanzar_estado(api_operario, reserva["id"], "acabado")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "acabado"
    assert avanzar_estado(api_operario, reserva["id"], "finalizado").status_code == 200


def test_desde_en_revision_se_puede_entregar(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """Anexo A v1.0: ``en_revision -> entregado``. El check-out no ramifica."""
    reserva = _finalizada_y_pagada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/revision", json={"observacion": "Una mancha"}
    )

    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "entregado"
    assert respuesta.json()["hora_entrega"] is not None


def test_la_revision_exige_una_observacion(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    reserva = _finalizada_y_pagada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )

    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/revision", json={"observacion": "   "}
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "VALIDACION"


def test_la_revision_de_un_servicio_no_terminado_responde_422(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id
):
    reserva = _reserva_confirmada(api_cliente, servicio_medio, vehiculo_id)

    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/revision", json={"observacion": "Algo"}
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"


def test_el_operario_no_puede_enviar_a_revision(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """La observación es del cliente y la recoge el mostrador (RF-024 3a)."""
    reserva = _finalizada_y_pagada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )

    respuesta = api_operario.post(
        f"{RUTA}/reservas/{reserva['id']}/revision", json={"observacion": "Algo"}
    )

    assert respuesta.status_code == 403


def test_la_entrega_registra_el_gancho_de_calificacion(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-024 paso 4: «habilita la calificación». INC-6 leerá este evento."""
    from app.models import EventoDominio

    reserva = _finalizada_y_pagada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )

    evento = db.scalars(
        select(EventoDominio).where(
            EventoDominio.accion == "reserva.calificacion_habilitada",
            EventoDominio.entidad_id == reserva["id"],
        )
    ).first()

    assert evento is not None
    assert evento.datos["habilitada_en"]
