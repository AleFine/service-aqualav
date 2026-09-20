"""Bay and operator assignment, and the waiting queue (RF-020).

``en_recepcion -> asignado`` was seeded by INC-1A with ``endpoint =
"asignacion"`` and no owner behind it; these tests exercise the owner INC-1B
built, including the two alternate flows: no free bay (2a) and a busy operator
(3a).
"""

from sqlalchemy import select

from app.models import AsignacionServicio, Bahia, ColaEspera, EventoDominio
from tests.conftest import (
    RUTA,
    asignar,
    codigo_error,
    crear_reserva,
    crear_vehiculo,
    instante,
    proximo_lunes,
)


def _recibida(api_cliente, api_recepcion, servicio, vehiculo_id, hora: int = 10) -> dict:
    """A reservation already checked in, i.e. waiting to be assigned."""
    reserva = crear_reserva(
        api_cliente, servicio.id, vehiculo_id, instante(proximo_lunes(dias_minimos=2), hora, 0)
    ).json()
    entrada = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": False}
    )
    assert entrada.status_code == 200, entrada.text
    return reserva


def _bahias(db) -> list[Bahia]:
    return list(db.scalars(select(Bahia).order_by(Bahia.id)).all())


def _dejar_bahias(db, cuantas: int) -> list[Bahia]:
    bahias = _bahias(db)
    for bahia in bahias[cuantas:]:
        bahia.activa = False
    db.commit()
    return bahias[:cuantas]


# --------------------------------------------------------------------------
# RF-020 CA-01 - a free bay becomes occupied
# --------------------------------------------------------------------------
def test_la_asignacion_ocupa_la_bahia(
    api_cliente, api_recepcion, api_admin, db, servicio_medio, vehiculo_id
):
    """RF-020 CA-01."""
    reserva = _recibida(api_cliente, api_recepcion, servicio_medio, vehiculo_id)

    respuesta = asignar(api_recepcion, reserva["id"])

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["reserva"]["estado"] == "asignado"
    assert cuerpo["cola"] is None
    assert cuerpo["asignacion"]["bahia"]["id"] == reserva["bahia"]["id"]
    assert cuerpo["asignacion"]["operario"]
    assert cuerpo["asignacion"]["sugerida"] is True

    bahias = {item["id"]: item for item in api_admin.get(f"{RUTA}/admin/bahias").json()["items"]}
    assert bahias[reserva["bahia"]["id"]]["estado"] == "ocupada"


def test_la_asignacion_deja_fila_y_evento(
    api_cliente, api_recepcion, db, servicio_medio, vehiculo_id
):
    """P7: quién asignó, a quién y cuándo queda escrito."""
    reserva = _recibida(api_cliente, api_recepcion, servicio_medio, vehiculo_id)

    asignar(api_recepcion, reserva["id"])

    fila = db.scalars(
        select(AsignacionServicio).where(AsignacionServicio.reserva_id == reserva["id"])
    ).first()
    assert fila is not None
    assert fila.asignado_por_id is not None
    assert fila.sugerida is True

    evento = db.scalars(
        select(EventoDominio).where(EventoDominio.accion == "reserva.asignada")
    ).first()
    assert evento is not None
    assert evento.datos["bahia_id"] == fila.bahia_id
    assert evento.datos["operario_id"] == fila.operario_id


# --------------------------------------------------------------------------
# RF-020 CA-02 - the service shows up in the operator's queue
# --------------------------------------------------------------------------
def test_el_servicio_aparece_en_la_cola_del_operario(
    api_cliente, api_recepcion, api_operario, servicio_medio, vehiculo_id
):
    """RF-020 CA-02."""
    reserva = _recibida(api_cliente, api_recepcion, servicio_medio, vehiculo_id)
    assert api_operario.get(f"{RUTA}/reservas/cola").json()["items"] == []

    asignar(api_recepcion, reserva["id"])

    respuesta = api_operario.get(f"{RUTA}/reservas/cola")

    assert respuesta.status_code == 200, respuesta.text
    assert [item["id"] for item in respuesta.json()["items"]] == [reserva["id"]]


def test_la_cola_se_vacia_cuando_el_servicio_termina(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """Entregado es terminal, así que sale de la cola del operario."""
    reserva = _recibida(api_cliente, api_recepcion, servicio_medio, vehiculo_id)
    asignar(api_recepcion, reserva["id"])
    for estado in ("en_lavado", "secado", "acabado", "finalizado"):
        api_operario.post(f"{RUTA}/reservas/{reserva['id']}/estado", json={"estado": estado})
    api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/pagos",
        json={"medio": "efectivo", "monto_centimos": 2500},
        headers={"Idempotency-Key": "cola-1"},
    )
    api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )

    assert api_operario.get(f"{RUTA}/reservas/cola").json()["items"] == []


# --------------------------------------------------------------------------
# RF-020 flow 2a - no free bay: the service waits with an estimate
# --------------------------------------------------------------------------
def test_sin_bahias_libres_el_servicio_queda_en_cola(
    api_cliente, api_recepcion, db, servicio_corto, vehiculo_id
):
    """RF-020 flujo 2a: la reserva NO se mueve, se encola con una estimación."""
    _dejar_bahias(db, 1)
    primera = _recibida(api_cliente, api_recepcion, servicio_corto, vehiculo_id, hora=10)
    otro = crear_vehiculo(api_cliente, "PQR-321")
    segunda = crear_reserva(
        api_cliente, servicio_corto.id, otro, instante(proximo_lunes(dias_minimos=2), 12, 0)
    ).json()
    api_recepcion.post(
        f"{RUTA}/reservas/{segunda['id']}/check-in", json={"confirmar_retraso": False}
    )

    assert asignar(api_recepcion, primera["id"]).json()["asignacion"] is not None

    respuesta = asignar(api_recepcion, segunda["id"])

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["asignacion"] is None
    assert cuerpo["reserva"]["estado"] == "en_recepcion", "la reserva no avanza sin bahía"
    assert cuerpo["cola"]["posicion"] == 1
    assert cuerpo["cola"]["tiempo_estimado_min"] >= 0
    assert db.scalars(select(ColaEspera)).first().reserva_id == segunda["id"]


def test_al_liberarse_la_bahia_la_reserva_encolada_se_asigna(
    api_cliente, api_recepcion, api_operario, db, servicio_corto, vehiculo_id
):
    """La salida del flujo 2a: cuando la bahía se libera, el mismo POST entra."""
    _dejar_bahias(db, 1)
    primera = _recibida(api_cliente, api_recepcion, servicio_corto, vehiculo_id, hora=10)
    otro = crear_vehiculo(api_cliente, "PQR-321")
    segunda = crear_reserva(
        api_cliente, servicio_corto.id, otro, instante(proximo_lunes(dias_minimos=2), 12, 0)
    ).json()
    api_recepcion.post(
        f"{RUTA}/reservas/{segunda['id']}/check-in", json={"confirmar_retraso": False}
    )
    asignar(api_recepcion, primera["id"])
    assert asignar(api_recepcion, segunda["id"]).json()["cola"] is not None

    for estado in ("en_lavado", "secado", "acabado", "finalizado"):
        api_operario.post(f"{RUTA}/reservas/{primera['id']}/estado", json={"estado": estado})
    api_recepcion.post(
        f"{RUTA}/reservas/{primera['id']}/pagos",
        json={"medio": "efectivo", "monto_centimos": 1500},
        headers={"Idempotency-Key": "libera-1"},
    )
    api_recepcion.post(
        f"{RUTA}/reservas/{primera['id']}/check-out", json={"conformidad_cliente": True}
    )

    respuesta = asignar(api_recepcion, segunda["id"])

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["asignacion"] is not None
    assert respuesta.json()["reserva"]["estado"] == "asignado"
    assert db.scalars(select(ColaEspera)).first() is None, "sale de la cola al asignarse"


# --------------------------------------------------------------------------
# RF-020 flow 3a - a busy operator needs an explicit confirmation
# --------------------------------------------------------------------------
def test_un_operario_ocupado_exige_confirmacion(
    api_cliente, api_recepcion, db, servicio_corto, vehiculo_id, usuario_operario
):
    """RF-020 flujo 3a, el mismo patrón que ``RETRASO_REQUIERE_CONFIRMACION``.

    El mostrador insiste en el operario que ya tiene trabajo: el sistema avisa
    y solo entonces obedece.
    """
    _dejar_bahias(db, 2)
    primera = _recibida(api_cliente, api_recepcion, servicio_corto, vehiculo_id, hora=10)
    otro = crear_vehiculo(api_cliente, "PQR-321")
    segunda = crear_reserva(
        api_cliente, servicio_corto.id, otro, instante(proximo_lunes(dias_minimos=2), 10, 0)
    ).json()
    api_recepcion.post(
        f"{RUTA}/reservas/{segunda['id']}/check-in", json={"confirmar_retraso": False}
    )
    asignada = asignar(api_recepcion, primera["id"]).json()
    assert asignada["asignacion"]["operario_id"] == usuario_operario.id

    respuesta = asignar(api_recepcion, segunda["id"], operario_id=usuario_operario.id)

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "OPERARIO_OCUPADO"
    assert str(usuario_operario.id) in str(respuesta.json()["error"]["detalles"])

    confirmada = asignar(
        api_recepcion,
        segunda["id"],
        operario_id=usuario_operario.id,
        confirmar_operario_ocupado=True,
    )

    assert confirmada.status_code == 200, confirmada.text
    assert confirmada.json()["reserva"]["estado"] == "asignado"


def test_el_administrador_no_es_el_operario_sugerido(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id, usuario_operario, usuario_admin
):
    """P5: el administrador PUEDE avanzar estados, pero no es quien lo hace.

    La sugerencia desempata por autoridad más estrecha, no por nombre de rol.
    """
    reserva = _recibida(api_cliente, api_recepcion, servicio_medio, vehiculo_id)

    cuerpo = asignar(api_recepcion, reserva["id"]).json()

    assert cuerpo["asignacion"]["operario_id"] == usuario_operario.id
    assert cuerpo["asignacion"]["operario_id"] != usuario_admin.id


def test_la_sugerencia_viaja_aunque_se_rechace_la_asignacion(
    api_cliente, api_recepcion, db, servicio_corto, vehiculo_id
):
    """Sin bahías libres el resultado sigue diciendo a quién sugeriría."""
    _dejar_bahias(db, 1)
    primera = _recibida(api_cliente, api_recepcion, servicio_corto, vehiculo_id, hora=10)
    otro = crear_vehiculo(api_cliente, "PQR-321")
    segunda = crear_reserva(
        api_cliente, servicio_corto.id, otro, instante(proximo_lunes(dias_minimos=2), 12, 0)
    ).json()
    api_recepcion.post(
        f"{RUTA}/reservas/{segunda['id']}/check-in", json={"confirmar_retraso": False}
    )
    asignar(api_recepcion, primera["id"])

    cuerpo = asignar(api_recepcion, segunda["id"]).json()

    assert cuerpo["sugerencia"]["bahia"] is None
    assert cuerpo["sugerencia"]["operario_id"] is not None
    assert cuerpo["sugerencia"]["operario"]


# --------------------------------------------------------------------------
# Overrides and refusals
# --------------------------------------------------------------------------
def test_se_puede_elegir_una_bahia_distinta(
    api_cliente, api_recepcion, db, servicio_medio, vehiculo_id
):
    reserva = _recibida(api_cliente, api_recepcion, servicio_medio, vehiculo_id)
    otra = [b for b in _bahias(db) if b.id != reserva["bahia"]["id"]][0]

    respuesta = asignar(api_recepcion, reserva["id"], bahia_id=otra.id)

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["asignacion"]["bahia"]["id"] == otra.id
    assert cuerpo["asignacion"]["sugerida"] is False, "el mostrador sobrescribió la sugerencia"
    assert cuerpo["reserva"]["bahia"]["id"] == otra.id, "la reserva se mudó de bahía"


def test_una_bahia_ocupada_no_se_puede_elegir(
    api_cliente, api_recepcion, db, servicio_corto, vehiculo_id
):
    _dejar_bahias(db, 2)
    primera = _recibida(api_cliente, api_recepcion, servicio_corto, vehiculo_id, hora=10)
    otro = crear_vehiculo(api_cliente, "PQR-321")
    segunda = crear_reserva(
        api_cliente, servicio_corto.id, otro, instante(proximo_lunes(dias_minimos=2), 10, 0)
    ).json()
    api_recepcion.post(
        f"{RUTA}/reservas/{segunda['id']}/check-in", json={"confirmar_retraso": False}
    )
    ocupada = asignar(api_recepcion, primera["id"]).json()["asignacion"]["bahia"]["id"]

    respuesta = asignar(
        api_recepcion, segunda["id"], bahia_id=ocupada, confirmar_operario_ocupado=True
    )

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "RESERVA_BLOQUE_OCUPADO"


def test_asignar_a_quien_no_es_operario_se_rechaza(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id, usuario_cliente
):
    """P5: "operario" se resuelve por permiso, no por nombre de rol."""
    reserva = _recibida(api_cliente, api_recepcion, servicio_medio, vehiculo_id)

    respuesta = asignar(api_recepcion, reserva["id"], operario_id=usuario_cliente.id)

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "DATOS_INVALIDOS"
    assert "reserva:avanzar_estado" in str(respuesta.json()["error"]["detalles"])


def test_asignar_una_reserva_sin_check_in_responde_422(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id
):
    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(dias_minimos=2), 10, 0)
    ).json()

    respuesta = asignar(api_recepcion, reserva["id"])

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"


def test_asignar_dos_veces_responde_422(api_cliente, api_recepcion, servicio_medio, vehiculo_id):
    reserva = _recibida(api_cliente, api_recepcion, servicio_medio, vehiculo_id)
    assert asignar(api_recepcion, reserva["id"]).status_code == 200

    respuesta = asignar(api_recepcion, reserva["id"])

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"


def test_la_asignacion_exige_su_permiso(
    api_cliente, api_recepcion, api_operario, servicio_medio, vehiculo_id
):
    reserva = _recibida(api_cliente, api_recepcion, servicio_medio, vehiculo_id)

    assert asignar(api_operario, reserva["id"]).status_code == 403
    assert asignar(api_cliente, reserva["id"]).status_code == 403


def test_la_cola_del_operario_exige_su_permiso(api_cliente, api_recepcion, cliente_http):
    assert cliente_http.get(f"{RUTA}/reservas/cola").status_code == 401
    assert api_cliente.get(f"{RUTA}/reservas/cola").status_code == 403
    assert api_recepcion.get(f"{RUTA}/reservas/cola").status_code == 403


# --------------------------------------------------------------------------
# The delivery gives the resources back
# --------------------------------------------------------------------------
def test_la_entrega_libera_la_bahia_y_borra_la_asignacion(
    api_cliente, api_recepcion, api_operario, api_admin, db, servicio_medio, vehiculo_id
):
    """RF-024 paso 4: "libera la bahía". La terminalidad la declara la tabla."""
    from tests.conftest import llevar_hasta_finalizado

    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(dias_minimos=2), 10, 0)
    ).json()
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])
    api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/pagos",
        json={"medio": "efectivo", "monto_centimos": 2500},
        headers={"Idempotency-Key": "entrega-1"},
    )

    entrega = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )

    assert entrega.status_code == 200, entrega.text
    bahias = {item["id"]: item for item in api_admin.get(f"{RUTA}/admin/bahias").json()["items"]}
    assert bahias[reserva["bahia"]["id"]]["estado"] == "libre"
    assert db.scalars(select(AsignacionServicio)).first() is None
