"""Operative agenda: opening calendar, blockings and the board (RF-018, RN-07).

The requirement that gives this module its reason to exist is ``RF-018 CA-01``:
"dada una franja bloqueada, cuando el cliente consulta disponibilidad, entonces
esa franja no aparece". Everything else - the week read from data, the
holidays, the board by bay - exists to serve it.
"""

from datetime import timedelta

from sqlalchemy import select

from app.core.horario import es_laborable
from app.models import Bahia, BloqueoFranja, DiaNoLaborable
from tests.conftest import (
    RUTA,
    codigo_error,
    crear_reserva,
    instante,
    proximo_domingo,
    proximo_lunes,
)


def _bahias(db) -> list[Bahia]:
    return list(db.scalars(select(Bahia).order_by(Bahia.id)).all())


def _bloquear(api, *, inicio, fin, bahia_id=None, motivo="mantenimiento", descripcion=None):
    cuerpo = {"inicio": inicio.isoformat(), "fin": fin.isoformat(), "motivo": motivo}
    if bahia_id is not None:
        cuerpo["bahia_id"] = bahia_id
    if descripcion is not None:
        cuerpo["descripcion"] = descripcion
    return api.post(f"{RUTA}/agenda/bloqueos", json=cuerpo)


def _disponibilidad(api, fecha, servicio):
    return api.get(
        f"{RUTA}/disponibilidad",
        params={"fecha": fecha.isoformat(), "servicio_id": servicio.id},
    )


def _horas(cuerpo) -> set[str]:
    return {bloque["inicio"][11:16] for bloque in cuerpo["bloques"]}


# --------------------------------------------------------------------------
# RF-018 CA-01 - a blocked slot disappears from the availability
# --------------------------------------------------------------------------
def test_una_franja_bloqueada_no_aparece_en_la_disponibilidad(
    api_cliente, api_admin, db, servicio_corto
):
    """RF-018 CA-01, el motivo de ser de toda la agenda.

    Con una sola bahía, bloquear 10:00-12:00 tiene que borrar esos bloques de
    la respuesta de RF-013 y dejar intactos los demás.
    """
    from tests.conftest import dejar_una_sola_bahia

    dejar_una_sola_bahia(db)
    fecha = proximo_lunes(dias_minimos=2)
    bahia = _bahias(db)[0]

    antes = _disponibilidad(api_cliente, fecha, servicio_corto)
    assert antes.status_code == 200, antes.text
    assert {"10:00", "10:30", "11:00", "11:30"} <= _horas(antes.json())

    bloqueo = _bloquear(
        api_admin,
        bahia_id=bahia.id,
        inicio=instante(fecha, 10, 0),
        fin=instante(fecha, 12, 0),
        descripcion="Cambio de bomba de agua",
    )
    assert bloqueo.status_code == 201, bloqueo.text

    despues = _disponibilidad(api_cliente, fecha, servicio_corto)

    assert despues.status_code == 200, despues.text
    horas = _horas(despues.json())
    # 09:45 termina a las 10:15 y también cae dentro de la franja bloqueada.
    assert not {hora for hora in horas if "09:45" <= hora < "12:00"}
    assert "09:30" in horas and "12:00" in horas, "el resto del día sigue en pie"


def test_un_bloqueo_de_todo_el_local_vacia_el_dia(api_cliente, api_admin, servicio_corto):
    """Un bloqueo sin bahía afecta a todas: el día se queda sin bloques."""
    fecha = proximo_lunes(dias_minimos=2)

    assert (
        _bloquear(
            api_admin,
            inicio=instante(fecha, 8, 0),
            fin=instante(fecha, 19, 0),
            motivo="ausencia",
            descripcion="Sin personal",
        ).status_code
        == 201
    )

    respuesta = _disponibilidad(api_cliente, fecha, servicio_corto)

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["bloques"] == []
    # Y, al cubrir toda la ventana, el día deja de ser laborable (RN-07 + RF-018).
    assert cuerpo["laborable"] is False
    assert cuerpo["siguiente_fecha_disponible"] is not None


def test_una_bahia_bloqueada_no_recibe_reservas(
    api_cliente, api_admin, db, servicio_corto, vehiculo_id
):
    """RF-018 CA-01 por la otra puerta: reservar tampoco entra en la franja."""
    bahias = _bahias(db)
    for bahia in bahias[2:]:
        bahia.activa = False
    db.commit()

    fecha = proximo_lunes(dias_minimos=2)
    assert (
        _bloquear(
            api_admin,
            bahia_id=bahias[0].id,
            inicio=instante(fecha, 8, 0),
            fin=instante(fecha, 19, 0),
        ).status_code
        == 201
    )

    respuesta = crear_reserva(api_cliente, servicio_corto.id, vehiculo_id, instante(fecha, 10, 0))

    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["bahia"]["id"] == bahias[1].id, "la bahía bloqueada no se reparte"


# --------------------------------------------------------------------------
# RF-018 CA-02 - resolve the bookings before blocking
# --------------------------------------------------------------------------
def test_bloquear_una_franja_con_reservas_exige_resolverlas(
    api_cliente, api_admin, db, servicio_corto, vehiculo_id
):
    """RF-018 CA-02 / flujo 4a."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = crear_reserva(
        api_cliente, servicio_corto.id, vehiculo_id, instante(fecha, 10, 0)
    ).json()

    respuesta = _bloquear(
        api_admin,
        bahia_id=reserva["bahia"]["id"],
        inicio=instante(fecha, 9, 0),
        fin=instante(fecha, 13, 0),
    )

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "FRANJA_CON_RESERVAS"
    assert reserva["codigo"] in str(respuesta.json()["error"]["detalles"])
    assert db.scalars(select(BloqueoFranja)).first() is None, "no se escribió nada"


def test_tras_cancelar_la_reserva_el_bloqueo_se_aplica(
    api_cliente, api_admin, servicio_corto, vehiculo_id
):
    """La salida que el flujo 4a describe: resolverlas y volver a intentar."""
    fecha = proximo_lunes(dias_minimos=2)
    reserva = crear_reserva(
        api_cliente, servicio_corto.id, vehiculo_id, instante(fecha, 10, 0)
    ).json()
    franja = {"inicio": instante(fecha, 9, 0), "fin": instante(fecha, 13, 0)}

    assert _bloquear(api_admin, bahia_id=reserva["bahia"]["id"], **franja).status_code == 409

    api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/cancelacion", json={"motivo": "Se reprograma"}
    )

    assert _bloquear(api_admin, bahia_id=reserva["bahia"]["id"], **franja).status_code == 201


def test_levantar_el_bloqueo_devuelve_los_bloques(api_cliente, api_admin, db, servicio_corto):
    from tests.conftest import dejar_una_sola_bahia

    dejar_una_sola_bahia(db)
    fecha = proximo_lunes(dias_minimos=2)
    creado = _bloquear(
        api_admin,
        bahia_id=_bahias(db)[0].id,
        inicio=instante(fecha, 10, 0),
        fin=instante(fecha, 12, 0),
    ).json()

    assert "10:00" not in _horas(_disponibilidad(api_cliente, fecha, servicio_corto).json())

    borrado = api_admin.delete(f"{RUTA}/agenda/bloqueos/{creado['id']}")

    assert borrado.status_code == 204
    assert "10:00" in _horas(_disponibilidad(api_cliente, fecha, servicio_corto).json())


# --------------------------------------------------------------------------
# RN-07 completed: holidays and the week as DATA
# --------------------------------------------------------------------------
def test_un_dia_no_laborable_deja_de_ofrecer_bloques(api_cliente, api_admin, servicio_corto):
    """RN-07 + RF-018: ``es_laborable`` ya no devuelve siempre ``True``."""
    fecha = proximo_lunes(dias_minimos=2)

    alta = api_admin.post(
        f"{RUTA}/agenda/dias-no-laborables",
        json={"fecha": fecha.isoformat(), "motivo": "Fiestas Patrias"},
    )
    assert alta.status_code == 201, alta.text

    respuesta = _disponibilidad(api_cliente, fecha, servicio_corto)

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["laborable"] is False
    assert cuerpo["bloques"] == []


def test_el_feriado_tambien_rechaza_la_creacion_de_la_reserva(
    api_cliente, api_admin, servicio_corto, vehiculo_id
):
    fecha = proximo_lunes(dias_minimos=2)
    api_admin.post(
        f"{RUTA}/agenda/dias-no-laborables",
        json={"fecha": fecha.isoformat(), "motivo": "Feriado local"},
    )

    respuesta = crear_reserva(api_cliente, servicio_corto.id, vehiculo_id, instante(fecha, 10, 0))

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "RESERVA_FUERA_DE_HORARIO"
    assert "Feriado local" in str(respuesta.json()["error"]["detalles"])


def test_reabrir_el_dia_no_laborable_devuelve_la_disponibilidad(
    api_cliente, api_admin, db, servicio_corto
):
    fecha = proximo_lunes(dias_minimos=2)
    creado = api_admin.post(
        f"{RUTA}/agenda/dias-no-laborables",
        json={"fecha": fecha.isoformat(), "motivo": "Se suspende"},
    ).json()

    borrado = api_admin.delete(f"{RUTA}/agenda/dias-no-laborables/{creado['id']}")

    assert borrado.status_code == 204
    assert db.scalars(select(DiaNoLaborable)).first() is None
    assert _disponibilidad(api_cliente, fecha, servicio_corto).json()["laborable"] is True


def test_el_horario_de_atencion_se_sirve_desde_la_tabla(api_recepcion):
    """RN-07 dejó de ser tres constantes: ahora se lee de ``horario_atencion``."""
    respuesta = api_recepcion.get(f"{RUTA}/agenda/horarios")

    assert respuesta.status_code == 200, respuesta.text
    por_dia = {item["dia_semana"]: item for item in respuesta.json()["items"]}
    assert len(por_dia) == 7
    assert por_dia[0]["hora_apertura"] == "08:00:00"
    assert por_dia[0]["hora_cierre"] == "19:00:00"
    assert por_dia[6]["hora_apertura"] == "09:00:00"
    assert por_dia[6]["hora_cierre"] == "14:00:00"
    assert all(not item["cerrado"] for item in por_dia.values())


def test_cerrar_un_dia_de_la_semana_es_un_cambio_de_datos(api_cliente, api_admin, servicio_corto):
    """El domingo deja de atender sin tocar una línea de código."""
    domingo = proximo_domingo(dias_minimos=2)
    assert _disponibilidad(api_cliente, domingo, servicio_corto).json()["bloques"]

    respuesta = api_admin.put(f"{RUTA}/agenda/horarios/6", json={})

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["cerrado"] is True
    cuerpo = _disponibilidad(api_cliente, domingo, servicio_corto).json()
    assert cuerpo["laborable"] is False
    assert cuerpo["bloques"] == []


def test_cambiar_el_horario_recorta_los_bloques(api_cliente, api_admin, servicio_corto):
    lunes = proximo_lunes(dias_minimos=2)

    respuesta = api_admin.put(
        f"{RUTA}/agenda/horarios/0",
        json={"hora_apertura": "10:00:00", "hora_cierre": "13:00:00"},
    )

    assert respuesta.status_code == 200, respuesta.text
    horas = _horas(_disponibilidad(api_cliente, lunes, servicio_corto).json())
    assert min(horas) == "10:00"
    assert max(horas) == "12:30", "un lavado de 30 min debe caber antes de las 13:00"


def test_el_horario_pide_las_dos_horas_juntas(api_admin):
    respuesta = api_admin.put(f"{RUTA}/agenda/horarios/0", json={"hora_apertura": "10:00:00"})

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "VALIDACION"


def test_es_laborable_sigue_siendo_puro_sin_calendario():
    """``app/core`` no abre sesiones: sin calendario responde la semana RN-07."""
    assert es_laborable(proximo_lunes()) is True


# --------------------------------------------------------------------------
# The board (RF-018: daily and weekly view, by bay)
# --------------------------------------------------------------------------
def test_la_agenda_diaria_muestra_la_reserva_en_su_bahia(
    api_cliente, api_recepcion, servicio_corto, vehiculo_id
):
    fecha = proximo_lunes(dias_minimos=2)
    reserva = crear_reserva(
        api_cliente, servicio_corto.id, vehiculo_id, instante(fecha, 10, 0)
    ).json()

    respuesta = api_recepcion.get(f"{RUTA}/agenda", params={"fecha": fecha.isoformat()})

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["vista"] == "dia"
    assert len(cuerpo["dias"]) == 1
    dia = cuerpo["dias"][0]
    assert dia["laborable"] is True
    assert dia["apertura"][11:16] == "08:00"
    ocupada = [bahia for bahia in dia["bahias"] if bahia["bahia"]["id"] == reserva["bahia"]["id"]][
        0
    ]
    assert [item["codigo"] for item in ocupada["reservas"]] == [reserva["codigo"]]
    assert ocupada["reservas"][0]["placa"] == "ABC-123"
    assert ocupada["reservas"][0]["operario"] is None, "aún no se asignó"


def test_la_agenda_semanal_cubre_los_siete_dias(api_recepcion, api_admin):
    fecha = proximo_lunes(dias_minimos=2)
    domingo = fecha + timedelta(days=6)
    api_admin.post(
        f"{RUTA}/agenda/dias-no-laborables",
        json={"fecha": domingo.isoformat(), "motivo": "Cierre anual"},
    )

    respuesta = api_recepcion.get(
        f"{RUTA}/agenda", params={"fecha": fecha.isoformat(), "vista": "semana"}
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["vista"] == "semana"
    assert len(cuerpo["dias"]) == 7
    assert cuerpo["desde"] == fecha.isoformat()
    ultimo = cuerpo["dias"][-1]
    assert ultimo["fecha"] == domingo.isoformat()
    assert ultimo["laborable"] is False
    assert ultimo["motivo_no_laborable"] == "Cierre anual"


def test_la_agenda_muestra_los_bloqueos_de_cada_bahia(api_admin, db):
    fecha = proximo_lunes(dias_minimos=2)
    bahia = _bahias(db)[0]
    _bloquear(
        api_admin,
        bahia_id=bahia.id,
        inicio=instante(fecha, 10, 0),
        fin=instante(fecha, 12, 0),
        descripcion="Mantenimiento programado",
    )

    cuerpo = api_admin.get(f"{RUTA}/agenda", params={"fecha": fecha.isoformat()}).json()

    con_bloqueo = [item for item in cuerpo["dias"][0]["bahias"] if item["bloqueos"]]
    assert [item["bahia"]["id"] for item in con_bloqueo] == [bahia.id]
    assert con_bloqueo[0]["bloqueos"][0]["descripcion"] == "Mantenimiento programado"
    assert con_bloqueo[0]["bloqueos"][0]["autor"]


# --------------------------------------------------------------------------
# Authorization (principle P5)
# --------------------------------------------------------------------------
def test_la_agenda_exige_su_permiso(api_cliente, api_operario, cliente_http):
    fecha = proximo_lunes().isoformat()

    assert cliente_http.get(f"{RUTA}/agenda", params={"fecha": fecha}).status_code == 401
    assert api_cliente.get(f"{RUTA}/agenda", params={"fecha": fecha}).status_code == 403
    assert api_operario.get(f"{RUTA}/agenda", params={"fecha": fecha}).status_code == 403


def test_el_recepcionista_puede_administrar_la_agenda(api_recepcion, db):
    """RF-018 nombra al recepcionista como actor: 0004 le dio el permiso."""
    fecha = proximo_lunes(dias_minimos=2)

    respuesta = _bloquear(
        api_recepcion,
        bahia_id=_bahias(db)[0].id,
        inicio=instante(fecha, 10, 0),
        fin=instante(fecha, 11, 0),
    )

    assert respuesta.status_code == 201, respuesta.text


def test_un_bloqueo_al_reves_se_rechaza(api_admin, db):
    fecha = proximo_lunes(dias_minimos=2)

    respuesta = _bloquear(
        api_admin,
        bahia_id=_bahias(db)[0].id,
        inicio=instante(fecha, 12, 0),
        fin=instante(fecha, 10, 0),
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "DATOS_INVALIDOS"


# --------------------------------------------------------------------------
# GET /agenda/bloqueos y GET /agenda/dias-no-laborables
# --------------------------------------------------------------------------
def test_el_listado_de_bloqueos_devuelve_los_del_rango_con_su_bahia(api_admin, db):
    """RF-018: sin este listado, un bloqueo se pone y no se puede levantar.

    ``DELETE /agenda/bloqueos/{id}`` necesita un id, y el id solo sale de
    aquí. Se comprueban las dos formas de bloqueo que admite el modelo —una
    bahía concreta y el local entero (``bahia`` nulo)— porque la pantalla las
    dibuja distinto.
    """
    bahias = _bahias(db)
    fecha = proximo_lunes(dias_minimos=4)

    de_una_bahia = _bloquear(
        api_admin,
        inicio=instante(fecha, 9, 0),
        fin=instante(fecha, 10, 0),
        bahia_id=bahias[0].id,
        descripcion="Cambio de filtros",
    )
    de_todo_el_local = _bloquear(
        api_admin, inicio=instante(fecha, 15, 0), fin=instante(fecha, 16, 0), motivo="ausencia"
    )
    assert de_una_bahia.status_code == 201, de_una_bahia.text
    assert de_todo_el_local.status_code == 201, de_todo_el_local.text

    respuesta = api_admin.get(f"{RUTA}/agenda/bloqueos", params={"desde": fecha.isoformat()})

    assert respuesta.status_code == 200, respuesta.text
    por_id = {fila["id"]: fila for fila in respuesta.json()["items"]}
    assert de_una_bahia.json()["id"] in por_id
    assert de_todo_el_local.json()["id"] in por_id

    concreto = por_id[de_una_bahia.json()["id"]]
    assert concreto["bahia"]["id"] == bahias[0].id
    assert concreto["motivo"] == "mantenimiento"
    assert concreto["descripcion"] == "Cambio de filtros"
    assert concreto["autor"], "quién lo bloqueó es parte del registro (RNF-014)"
    assert por_id[de_todo_el_local.json()["id"]]["bahia"] is None, "todo el local"


def test_el_listado_de_bloqueos_respeta_el_rango_pedido(api_admin, db):
    """Un rango que no es el del bloqueo no lo trae.

    Si el filtro no se aplicara, la pantalla de una semana mostraría los
    bloqueos de todas y el test anterior pasaría igual.
    """
    fecha = proximo_lunes(dias_minimos=4)
    lejos = fecha + timedelta(days=21)
    bloqueo = _bloquear(api_admin, inicio=instante(lejos, 9, 0), fin=instante(lejos, 10, 0))
    assert bloqueo.status_code == 201, bloqueo.text

    cercano = api_admin.get(
        f"{RUTA}/agenda/bloqueos",
        params={"desde": fecha.isoformat(), "hasta": (fecha + timedelta(days=1)).isoformat()},
    )
    propio = api_admin.get(f"{RUTA}/agenda/bloqueos", params={"desde": lejos.isoformat()})

    assert bloqueo.json()["id"] not in {f["id"] for f in cercano.json()["items"]}
    assert bloqueo.json()["id"] in {f["id"] for f in propio.json()["items"]}


def test_el_listado_de_dias_no_laborables_devuelve_los_feriados_declarados(api_admin):
    """RF-018 / RN-07: el calendario de cierres, legible antes de poder editarlo."""
    fecha = proximo_lunes(dias_minimos=10)

    creado = api_admin.post(
        f"{RUTA}/agenda/dias-no-laborables",
        json={"fecha": fecha.isoformat(), "motivo": "Feriado de prueba"},
    )
    assert creado.status_code == 201, creado.text

    respuesta = api_admin.get(f"{RUTA}/agenda/dias-no-laborables")

    assert respuesta.status_code == 200, respuesta.text
    filas = {fila["fecha"]: fila for fila in respuesta.json()["items"]}
    assert filas[fecha.isoformat()]["motivo"] == "Feriado de prueba"
    assert filas[fecha.isoformat()]["autor"], "quién lo declaró (RNF-014)"


def test_el_listado_de_dias_no_laborables_filtra_por_rango(api_admin):
    """El mismo filtro que los bloqueos, y por la misma razón."""
    cerca = proximo_lunes(dias_minimos=10)
    lejos = cerca + timedelta(days=30)
    for fecha, motivo in ((cerca, "Cercano"), (lejos, "Lejano")):
        creado = api_admin.post(
            f"{RUTA}/agenda/dias-no-laborables",
            json={"fecha": fecha.isoformat(), "motivo": motivo},
        )
        assert creado.status_code == 201, creado.text

    acotado = api_admin.get(
        f"{RUTA}/agenda/dias-no-laborables",
        params={"desde": cerca.isoformat(), "hasta": (cerca + timedelta(days=1)).isoformat()},
    )

    fechas = {fila["fecha"] for fila in acotado.json()["items"]}
    assert cerca.isoformat() in fechas
    assert lejos.isoformat() not in fechas


def test_leer_la_agenda_exige_agenda_leer(api_cliente, api_operario, cliente_http):
    """RF-018: sus actores son el administrador y el mostrador, nadie más.

    El cliente ve la DISPONIBILIDAD (lo libre); la agenda —lo ocupado, con
    nombre de cliente y de bahía— es información de taller.
    """
    hoy = proximo_lunes().isoformat()

    assert api_cliente.get(f"{RUTA}/agenda/bloqueos", params={"desde": hoy}).status_code == 403
    assert api_cliente.get(f"{RUTA}/agenda/dias-no-laborables").status_code == 403
    assert api_operario.get(f"{RUTA}/agenda/dias-no-laborables").status_code == 403
    assert cliente_http.get(f"{RUTA}/agenda/dias-no-laborables").status_code == 401


def test_el_mostrador_lee_la_agenda_porque_RF_018_lo_nombra(api_recepcion, db):
    """RF-018 nombra al recepcionista junto al administrador."""
    fecha = proximo_lunes(dias_minimos=4)

    assert (
        api_recepcion.get(
            f"{RUTA}/agenda/bloqueos", params={"desde": fecha.isoformat()}
        ).status_code
        == 200
    )
    assert api_recepcion.get(f"{RUTA}/agenda/dias-no-laborables").status_code == 200
