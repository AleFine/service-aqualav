"""Vehicle registration, edition and logical deletion (RF-007, RF-008, RN-01)."""

from app.config import settings
from tests.conftest import (
    RUTA,
    codigo_error,
    crear_reserva,
    crear_vehiculo,
    instante,
    proximo_lunes,
)

NUEVO = {
    "placa": "xyz-789",
    "tipo": "suv",
    "marca": "Kia",
    "modelo": "Sportage",
    "color": "Negro",
    "anio": 2021,
}


def test_listar_devuelve_los_vehiculos_propios(api_cliente):
    respuesta = api_cliente.get(f"{RUTA}/vehiculos")

    assert respuesta.status_code == 200, respuesta.text
    placas = [item["placa"] for item in respuesta.json()["items"]]
    assert "ABC-123" in placas


def test_registrar_un_vehiculo_responde_201(api_cliente):
    """RF-007 CA-01: a valid plate is accepted and appears on the list."""
    respuesta = api_cliente.post(f"{RUTA}/vehiculos", json=NUEVO)

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    # The plate is normalized to uppercase before it is stored.
    assert cuerpo["placa"] == "XYZ-789"

    listado = api_cliente.get(f"{RUTA}/vehiculos").json()["items"]
    assert any(item["placa"] == "XYZ-789" for item in listado)


def test_el_tipo_queda_persistido(api_cliente):
    """RF-007 CA-03: ``tipo`` is captured now because RF-012 needs it in v0.3."""
    creado = api_cliente.post(f"{RUTA}/vehiculos", json=NUEVO).json()

    listado = api_cliente.get(f"{RUTA}/vehiculos").json()["items"]
    guardado = next(item for item in listado if item["id"] == creado["id"])
    assert guardado["tipo"] == "suv"


def test_placa_duplicada_en_la_cuenta_responde_409(api_cliente):
    """RF-007 CA-02."""
    assert api_cliente.post(f"{RUTA}/vehiculos", json=NUEVO).status_code == 201

    respuesta = api_cliente.post(f"{RUTA}/vehiculos", json=NUEVO)

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "PLACA_DUPLICADA"


def test_placa_con_formato_invalido_responde_422(api_cliente):
    """RF-007 flow 4a: the expected pattern is spelled out in ``detalles``."""
    respuesta = api_cliente.post(f"{RUTA}/vehiculos", json=dict(NUEVO, placa="A-1"))

    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["detalles"]


def test_dos_clientes_pueden_usar_la_misma_placa(api_cliente, cliente_http):
    """The uniqueness key is ``(usuario_id, placa)``, not the plate alone."""
    crear_vehiculo(api_cliente, "XYZ-789")

    cliente_http.post(
        f"{RUTA}/auth/registro",
        json={
            "nombres": "Beto",
            "apellidos": "Ruiz",
            "correo": "beto@example.com",
            "telefono": "987111222",
            "tipo_documento": "dni",
            "numero_documento": "71111222",
            "password": "Aqua1234",
            "acepta_politica": True,
        },
    )
    token = cliente_http.post(
        f"{RUTA}/auth/login", json={"correo": "beto@example.com", "password": "Aqua1234"}
    ).json()["access_token"]

    respuesta = cliente_http.post(
        f"{RUTA}/vehiculos", json=NUEVO, headers={"Authorization": f"Bearer {token}"}
    )

    assert respuesta.status_code == 201


def test_vehiculos_sin_token_responde_401(cliente_http):
    assert cliente_http.get(f"{RUTA}/vehiculos").status_code == 401


def test_tipo_invalido_responde_en_espanol(api_cliente):
    """RNF-009 M3: los rechazos de enum de Pydantic también salen traducidos."""
    respuesta = api_cliente.post(f"{RUTA}/vehiculos", json=dict(NUEVO, tipo="camion"))

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "VALIDACION"

    detalles = respuesta.json()["error"]["detalles"]
    mensaje = next(d["mensaje"] for d in detalles if d["campo"] == "tipo")
    assert "Input should be" not in mensaje
    assert mensaje == "Valor no válido. Usa uno de: sedan, suv, camioneta, motocicleta."


# --------------------------------------------------------------------------
# RF-008 - edition and logical deletion
# --------------------------------------------------------------------------
def _reservar(api_cliente, servicio_medio, vehiculo: int):
    return crear_reserva(api_cliente, servicio_medio.id, vehiculo, instante(proximo_lunes(), 10, 0))


def test_editar_un_vehiculo_guarda_los_cambios(api_cliente):
    """RF-008 salidas: «vehículo actualizado, sin pérdida de historial»."""
    creado = api_cliente.post(f"{RUTA}/vehiculos", json=NUEVO).json()

    respuesta = api_cliente.patch(
        f"{RUTA}/vehiculos/{creado['id']}", json={"color": "Blanco", "anio": 2022}
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["color"] == "Blanco"
    assert respuesta.json()["anio"] == 2022
    # Lo que no se envía no se toca.
    assert respuesta.json()["placa"] == "XYZ-789"


def test_editar_no_permite_chocar_con_otra_placa_propia(api_cliente):
    """RF-007 CA-02 sigue valiendo al editar: la placa es única en la cuenta."""
    creado = api_cliente.post(f"{RUTA}/vehiculos", json=NUEVO).json()

    respuesta = api_cliente.patch(f"{RUTA}/vehiculos/{creado['id']}", json={"placa": "ABC-123"})

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "PLACA_DUPLICADA"


def test_editar_el_vehiculo_de_otro_responde_404(api_cliente, api_recepcion, db):
    """Autorización horizontal: un 404, nunca un 403 que confirme el id ajeno."""
    from app.models import Vehiculo

    ajeno = Vehiculo(
        usuario_id=2, placa="AAA-111", tipo="sedan", marca="X", modelo="Y", color="Z", anio=2020
    )
    db.add(ajeno)
    db.commit()

    respuesta = api_cliente.patch(f"{RUTA}/vehiculos/{ajeno.id}", json={"color": "Rojo"})

    assert respuesta.status_code == 404


def test_dar_de_baja_lo_saca_de_la_lista_activa(api_cliente):
    """RF-008 CA-01: «desaparece de la lista activa»."""
    creado = api_cliente.post(f"{RUTA}/vehiculos", json=NUEVO).json()

    respuesta = api_cliente.delete(f"{RUTA}/vehiculos/{creado['id']}")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["activo"] is False
    activos = api_cliente.get(f"{RUTA}/vehiculos").json()["items"]
    assert all(item["id"] != creado["id"] for item in activos)
    # Sigue existiendo: la baja es lógica (flujo 4a).
    con_bajas = api_cliente.get(f"{RUTA}/vehiculos?incluir_inactivos=true").json()["items"]
    assert any(item["id"] == creado["id"] for item in con_bajas)


def test_el_historial_del_vehiculo_dado_de_baja_sigue_consultable(api_cliente, servicio_medio, db):
    """RF-008 CA-02: sus servicios previos siguen visibles."""
    vehiculo = crear_vehiculo(api_cliente, "XYZ-789")
    reserva = _reservar(api_cliente, servicio_medio, vehiculo).json()
    api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/cancelacion", json={"motivo": "Cambio de planes"}
    )

    assert api_cliente.delete(f"{RUTA}/vehiculos/{vehiculo}").status_code == 200

    historial = api_cliente.get(f"{RUTA}/reservas").json()["items"]
    assert any(item["id"] == reserva["id"] for item in historial)
    detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}")
    assert detalle.status_code == 200
    assert detalle.json()["vehiculo"]["placa"] == "XYZ-789"


def test_con_reserva_vigente_la_baja_informa_cual(api_cliente, servicio_medio):
    """RF-008 flujo 3a: se impide la baja e informa la reserva asociada."""
    vehiculo = crear_vehiculo(api_cliente, "XYZ-789")
    reserva = _reservar(api_cliente, servicio_medio, vehiculo).json()

    respuesta = api_cliente.delete(f"{RUTA}/vehiculos/{vehiculo}")

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "VEHICULO_CON_RESERVA_VIGENTE"
    detalles = respuesta.json()["error"]["detalles"]
    assert any(reserva["codigo"] in item["mensaje"] for item in detalles)


def test_un_vehiculo_dado_de_baja_ya_no_puede_reservar(api_cliente, servicio_medio):
    """La baja lógica retira el vehículo de las reservas NUEVAS."""
    vehiculo = crear_vehiculo(api_cliente, "XYZ-789")
    assert api_cliente.delete(f"{RUTA}/vehiculos/{vehiculo}").status_code == 200

    respuesta = _reservar(api_cliente, servicio_medio, vehiculo)

    assert respuesta.status_code == 404
    assert codigo_error(respuesta) == "RECURSO_NO_ENCONTRADO"


# --------------------------------------------------------------------------
# RN-01 - "registrado y verificado"
# --------------------------------------------------------------------------
def test_un_vehiculo_nace_sin_verificar_y_recepcion_lo_verifica(api_cliente, api_recepcion):
    """RN-01 v1.0: verificar es un acto del local, con su propio permiso."""
    creado = api_cliente.post(f"{RUTA}/vehiculos", json=NUEVO).json()
    assert creado["verificado"] is False

    respuesta = api_recepcion.post(f"{RUTA}/vehiculos/{creado['id']}/verificacion")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["verificado"] is True


def test_el_cliente_no_puede_verificar_su_propio_vehiculo(api_cliente):
    """P5: la verificación se decide por permiso, y el cliente no lo tiene."""
    creado = api_cliente.post(f"{RUTA}/vehiculos", json=NUEVO).json()

    respuesta = api_cliente.post(f"{RUTA}/vehiculos/{creado['id']}/verificacion")

    assert respuesta.status_code == 403
    assert codigo_error(respuesta) == "PERMISO_DENEGADO"


def test_rn01_puede_exigir_vehiculo_verificado_para_reservar(
    api_cliente, api_recepcion, servicio_medio, monkeypatch
):
    """RN-01: «al menos un vehículo registrado Y VERIFICADO» antes de reservar.

    La regla está implementada y el interruptor viene apagado: ningún requisito
    describe cómo se verifica un vehículo ANTES de su primera visita, así que
    exigirlo de fábrica dejaría a un cliente nuevo sin poder reservar la cita
    que permitiría al mostrador verificar su auto (RN-01 contra RF-019).
    """
    monkeypatch.setattr(settings, "exigir_vehiculo_verificado", True)
    vehiculo = crear_vehiculo(api_cliente, "XYZ-789")

    rechazada = _reservar(api_cliente, servicio_medio, vehiculo)

    assert rechazada.status_code == 422
    assert codigo_error(rechazada) == "VEHICULO_NO_VERIFICADO"

    assert api_recepcion.post(f"{RUTA}/vehiculos/{vehiculo}/verificacion").status_code == 200
    assert _reservar(api_cliente, servicio_medio, vehiculo).status_code == 201


def test_con_el_interruptor_apagado_un_vehiculo_nuevo_reserva(api_cliente, servicio_medio):
    """El valor por defecto: reservar no exige verificación (236 pruebas lo asumen)."""
    assert settings.exigir_vehiculo_verificado is False
    vehiculo = crear_vehiculo(api_cliente, "XYZ-789")

    assert _reservar(api_cliente, servicio_medio, vehiculo).status_code == 201
