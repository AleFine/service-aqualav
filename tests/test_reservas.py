"""Reservation creation, history and cancellation (RF-014, RF-016, RF-017)."""

import re
from datetime import datetime, timedelta

from app.core.horario import ahora
from tests.conftest import (
    RUTA,
    codigo_error,
    crear_reserva,
    crear_vehiculo,
    dejar_una_sola_bahia,
    instante,
    proximo_lunes,
)

CODIGO_RESERVA = re.compile(r"^AQL-[A-Z2-9]{6}$")


def test_crear_una_reserva_responde_201_con_su_codigo(api_cliente, servicio_medio, vehiculo_id):
    """RF-014 CA-01."""
    fecha = proximo_lunes()

    respuesta = crear_reserva(api_cliente, servicio_medio.id, vehiculo_id, instante(fecha, 10, 0))

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert CODIGO_RESERVA.match(cuerpo["codigo"]), cuerpo["codigo"]
    assert cuerpo["estado"] == "confirmada"
    assert cuerpo["modalidad_pago"] == "presencial"
    assert cuerpo["monto"] == {"monto_centimos": 2500, "moneda": "PEN"}
    assert cuerpo["bahia"]["id"] is not None
    # 10:00 + 45 minutes.
    assert datetime.fromisoformat(cuerpo["fin"]).hour == 10
    assert datetime.fromisoformat(cuerpo["fin"]).minute == 45
    # One history row is written at creation (EXTENSION POINT P7).
    assert [fila["estado"] for fila in cuerpo["historial"]] == ["confirmada"]
    # The customer may cancel, but check-in is staff-only, so the app only
    # renders the cancel button.
    assert cuerpo["transiciones_permitidas"] == ["cancelada"]


def test_la_tarifa_queda_congelada_al_crear(api_cliente, api_admin, servicio_medio, vehiculo_id):
    """RF-014 CA-03 y RF-010 CA-02: subir el precio no toca la reserva vieja."""
    fecha = proximo_lunes()
    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(fecha, 10, 0)
    ).json()

    api_admin.patch(f"{RUTA}/admin/servicios/{servicio_medio.id}", json={"monto_centimos": 9900})

    detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
    assert detalle["monto"]["monto_centimos"] == 2500

    catalogo = api_cliente.get(f"{RUTA}/servicios/{servicio_medio.id}").json()
    assert catalogo["precio"]["monto_centimos"] == 9900


def test_una_reserva_con_menos_de_una_hora_de_anticipacion_falla(
    api_cliente, servicio_medio, vehiculo_id
):
    """RN-02."""
    respuesta = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, ahora() + timedelta(minutes=10)
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "RESERVA_ANTICIPACION_INSUFICIENTE"


def test_una_reserva_fuera_del_horario_de_atencion_falla(api_cliente, servicio_medio, vehiculo_id):
    """RN-07: el intervalo completo debe caber en la ventana del día."""
    respuesta = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 22, 0)
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "RESERVA_FUERA_DE_HORARIO"


def test_una_reserva_que_termina_despues_del_cierre_falla(api_cliente, servicio_medio, vehiculo_id):
    """RN-07: 18:45 + 45 minutos se pasa de las 19:00."""
    respuesta = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 18, 45)
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "RESERVA_FUERA_DE_HORARIO"


def test_no_se_puede_reservar_con_un_vehiculo_ajeno(api_cliente, servicio_medio):
    """RN-01."""
    respuesta = crear_reserva(api_cliente, servicio_medio.id, 9999, instante(proximo_lunes()))

    assert respuesta.status_code == 404
    assert codigo_error(respuesta) == "RECURSO_NO_ENCONTRADO"


def test_dos_reservas_del_mismo_bloque_dan_un_201_y_un_409(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-014 CA-02.

    ``SELECT ... FOR UPDATE`` no hace nada en SQLite, así que la prueba afirma
    el RESULTADO (exactamente un 201 y un 409), no el mecanismo de bloqueo.
    """
    dejar_una_sola_bahia(db)
    otro_vehiculo = crear_vehiculo(api_cliente, "XYZ-789")
    inicio = instante(proximo_lunes(), 10, 0)

    primera = crear_reserva(api_cliente, servicio_medio.id, vehiculo_id, inicio)
    segunda = crear_reserva(api_cliente, servicio_medio.id, otro_vehiculo, inicio)

    assert primera.status_code == 201, primera.text
    assert segunda.status_code == 409
    assert codigo_error(segunda) == "RESERVA_BLOQUE_OCUPADO"
    # El 409 ofrece bloques cercanos (RF-014 flujo 3a).
    assert segunda.json()["error"]["detalles"]


def test_las_bahias_se_asignan_de_menor_a_mayor_y_la_quinta_choca(
    api_cliente, servicio_medio, vehiculo_id
):
    """RN-03: cuatro bahías, cuatro reservas simultáneas; la quinta no entra."""
    inicio = instante(proximo_lunes(), 10, 0)
    vehiculos = [vehiculo_id] + [
        crear_vehiculo(api_cliente, placa) for placa in ("XYZ-789", "JKL-456", "MNO-321")
    ]

    bahias = []
    for identificador in vehiculos:
        respuesta = crear_reserva(api_cliente, servicio_medio.id, identificador, inicio)
        assert respuesta.status_code == 201, respuesta.text
        bahias.append(respuesta.json()["bahia"]["id"])

    assert bahias == sorted(bahias) and len(set(bahias)) == 4

    quinto = crear_vehiculo(api_cliente, "PQR-654")
    respuesta = crear_reserva(api_cliente, servicio_medio.id, quinto, inicio)

    assert respuesta.status_code == 409


def test_el_historial_se_pagina_de_veinte_en_veinte(api_cliente, servicio_medio, vehiculo_id):
    """RF-017 CA-01: el tamaño por defecto es 20 y el máximo 50."""
    fecha = proximo_lunes()
    for hora in (10, 11, 12):
        assert (
            crear_reserva(
                api_cliente, servicio_medio.id, vehiculo_id, instante(fecha, hora, 0)
            ).status_code
            == 201
        )

    completo = api_cliente.get(f"{RUTA}/reservas").json()
    assert completo["tamanio"] == 20
    assert completo["total"] == 3
    # Ordenadas por inicio descendente.
    assert datetime.fromisoformat(completo["items"][0]["inicio"]).hour == 12

    pagina = api_cliente.get(f"{RUTA}/reservas", params={"tamanio": 2, "pagina": 2}).json()
    assert pagina["tamanio"] == 2
    assert pagina["total_paginas"] == 2
    assert len(pagina["items"]) == 1


def test_el_filtro_por_estado_solo_devuelve_ese_estado(api_cliente, servicio_medio, vehiculo_id):
    """RF-017 CA-02."""
    fecha = proximo_lunes()
    primera = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(fecha, 10, 0)
    ).json()
    crear_reserva(api_cliente, servicio_medio.id, vehiculo_id, instante(fecha, 12, 0))

    api_cliente.post(
        f"{RUTA}/reservas/{primera['id']}/cancelacion", json={"motivo": "Cambio de planes"}
    )

    canceladas = api_cliente.get(f"{RUTA}/reservas", params={"estado": "cancelada"}).json()
    confirmadas = api_cliente.get(f"{RUTA}/reservas", params={"estado": "confirmada"}).json()

    assert canceladas["total"] == 1
    assert canceladas["items"][0]["id"] == primera["id"]
    assert confirmadas["total"] == 1


def test_un_cliente_no_ve_las_reservas_de_otro(
    api_cliente, cliente_http, servicio_medio, vehiculo_id
):
    """RF-017 CA-03: autorización horizontal."""
    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    ).json()

    cliente_http.post(
        f"{RUTA}/auth/registro",
        json={
            "nombres": "Beto",
            "apellidos": "Ruiz",
            "correo": "beto@example.com",
            "telefono": "987111222",
            "password": "Aqua1234",
            "acepta_politica": True,
        },
    )
    token = cliente_http.post(
        f"{RUTA}/auth/login", json={"correo": "beto@example.com", "password": "Aqua1234"}
    ).json()["access_token"]
    cabeceras = {"Authorization": f"Bearer {token}"}

    listado = cliente_http.get(f"{RUTA}/reservas", headers=cabeceras).json()
    detalle = cliente_http.get(f"{RUTA}/reservas/{reserva['id']}", headers=cabeceras)

    assert listado["total"] == 0
    assert detalle.status_code == 404


def test_el_personal_si_ve_las_reservas_de_todos(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """``reserva:leer_todas`` levanta el filtro horizontal."""
    crear_reserva(api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10, 0))

    listado = api_personal.get(f"{RUTA}/reservas").json()

    assert listado["total"] == 1


def test_cancelar_registra_motivo_autor_y_fecha(api_cliente, servicio_medio, vehiculo_id):
    """RF-016 CA-03, con la penalidad de la política vigente (cero en el MVP)."""
    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    ).json()

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/cancelacion", json={"motivo": "Se me cruzó una reunión"}
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "cancelada"
    assert cuerpo["cancelacion"]["motivo"] == "Se me cruzó una reunión"
    assert cuerpo["cancelacion"]["autor"]
    assert cuerpo["cancelacion"]["cancelada_en"]
    assert cuerpo["penalidad"] == {"monto_centimos": 0, "moneda": "PEN"}
    assert [fila["estado"] for fila in cuerpo["historial"]] == ["confirmada", "cancelada"]


def test_el_bloque_cancelado_vuelve_a_ofrecerse(api_cliente, db, servicio_corto, vehiculo_id):
    """RF-016 CA-01."""
    dejar_una_sola_bahia(db)
    fecha = proximo_lunes()
    reserva = crear_reserva(
        api_cliente, servicio_corto.id, vehiculo_id, instante(fecha, 10, 0)
    ).json()

    def bloques():
        cuerpo = api_cliente.get(
            f"{RUTA}/disponibilidad",
            params={"fecha": fecha.isoformat(), "servicio_id": servicio_corto.id},
        ).json()
        return {datetime.fromisoformat(b["inicio"]).strftime("%H:%M") for b in cuerpo["bloques"]}

    assert "10:00" not in bloques()

    api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/cancelacion", json={"motivo": "Ya no puedo ir"}
    )

    assert "10:00" in bloques()


def test_cancelar_una_reserva_en_atencion_responde_422(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """RF-016 CA-02: la transición no existe en ``transicion_estado``."""
    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    ).json()
    api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": False}
    )

    respuesta = api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/cancelacion", json={"motivo": "Ya no"}
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "TRANSICION_INVALIDA"


# --------------------------------------------------------------------------
# C3 - the bay is held until the delivery, not until the service ends
# --------------------------------------------------------------------------
def test_reserva_finalizada_retiene_su_bahia_hasta_la_entrega(
    api_cliente, api_personal, db, servicio_corto, vehiculo_id
):
    """RF-024 CA-02: la bahía se libera *dada una entrega registrada*.

    Antes de C3 ``finalizado`` no contaba como activo, así que el coche seguía
    dentro del local y su bahía ya se ofrecía de nuevo (RN-03).
    """
    dejar_una_sola_bahia(db)
    fecha = proximo_lunes()
    inicio = instante(fecha, 10, 0)

    reserva = crear_reserva(api_cliente, servicio_corto.id, vehiculo_id, inicio).json()
    api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": False}
    )
    assert (
        api_personal.post(
            f"{RUTA}/reservas/{reserva['id']}/estado", json={"estado": "finalizado"}
        ).status_code
        == 200
    )

    otro = crear_vehiculo(api_cliente, "PQR-321")
    choque = crear_reserva(api_cliente, servicio_corto.id, otro, inicio)

    assert (
        choque.status_code == 409
    ), "RF-024 CA-02: la bahía sigue ocupada hasta que se registre la entrega"
    assert codigo_error(choque) == "RESERVA_BLOQUE_OCUPADO"

    # Tras el cobro y la entrega, el bloque vuelve a ofrecerse.
    api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/pagos",
        json={"medio": "efectivo", "monto_centimos": 1500},
        headers={"Idempotency-Key": "entrega-libera"},
    )
    assert (
        api_personal.post(
            f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
        ).status_code
        == 200
    )

    assert crear_reserva(api_cliente, servicio_corto.id, otro, inicio).status_code == 201
