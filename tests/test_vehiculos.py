"""Vehicle registration (RF-007)."""

from tests.conftest import RUTA, codigo_error, crear_vehiculo

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
