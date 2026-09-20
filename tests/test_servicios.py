"""Service catalog and tariffs (RF-009, RF-010)."""

from sqlalchemy import select

from app.models import ServicioPrecio
from tests.conftest import RUTA, codigo_error

NUEVO = {
    "nombre": "Lavado de faros",
    "descripcion": "Pulido y abrillantado de faros delanteros.",
    "categoria": "especializado",
    "duracion_min": 30,
    "monto_centimos": 3000,
    "moneda": "PEN",
}


def test_el_catalogo_expone_el_precio_como_objeto(api_cliente):
    """RF-009 CA-02: ``precio`` is an object so v0.3 can grow it (P6)."""
    respuesta = api_cliente.get(f"{RUTA}/servicios")

    assert respuesta.status_code == 200, respuesta.text
    servicio = respuesta.json()["items"][0]
    assert set(servicio["precio"]) == {"monto_centimos", "moneda"}
    assert servicio["precio"]["moneda"] == "PEN"
    assert servicio["precio"]["monto_centimos"] > 0


def test_el_catalogo_oculta_los_servicios_inactivos(api_cliente, api_admin, servicio_corto):
    """RF-009 CA-01 and RF-010 CA-03."""
    visibles = api_cliente.get(f"{RUTA}/servicios").json()["items"]
    assert any(item["id"] == servicio_corto.id for item in visibles)

    api_admin.patch(f"{RUTA}/admin/servicios/{servicio_corto.id}", json={"activo": False})

    visibles = api_cliente.get(f"{RUTA}/servicios").json()["items"]
    assert all(item["id"] != servicio_corto.id for item in visibles)
    # ... and the detail endpoint hides it too.
    assert api_cliente.get(f"{RUTA}/servicios/{servicio_corto.id}").status_code == 404


def test_el_administrador_si_ve_los_inactivos(api_admin, servicio_corto):
    """RF-010: administration lists the whole catalog."""
    api_admin.patch(f"{RUTA}/admin/servicios/{servicio_corto.id}", json={"activo": False})

    items = api_admin.get(f"{RUTA}/admin/servicios").json()["items"]

    inactivo = next(item for item in items if item["id"] == servicio_corto.id)
    assert inactivo["activo"] is False


def test_crear_un_servicio_lo_publica_en_el_catalogo(api_admin, api_cliente):
    """RF-010 CA-01."""
    respuesta = api_admin.post(f"{RUTA}/admin/servicios", json=NUEVO)

    assert respuesta.status_code == 201, respuesta.text
    creado = respuesta.json()
    assert creado["precio"]["monto_centimos"] == 3000

    visibles = api_cliente.get(f"{RUTA}/servicios").json()["items"]
    assert any(item["id"] == creado["id"] for item in visibles)


def test_rechaza_una_duracion_que_no_es_multiplo_de_quince(api_admin):
    """RF-010 flow 4a."""
    respuesta = api_admin.post(f"{RUTA}/admin/servicios", json=dict(NUEVO, duracion_min=40))

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "VALIDACION"


def test_rechaza_un_monto_no_positivo(api_admin):
    """RF-010 flow 4a."""
    respuesta = api_admin.post(f"{RUTA}/admin/servicios", json=dict(NUEVO, monto_centimos=0))

    assert respuesta.status_code == 422


def test_cambiar_el_precio_abre_una_nueva_vigencia(api_admin, db, servicio_medio):
    """RF-010 CA-02 / EXTENSION POINT P6: the amount is never overwritten."""
    anterior = servicio_medio.precio_vigente.monto_centimos

    respuesta = api_admin.patch(
        f"{RUTA}/admin/servicios/{servicio_medio.id}", json={"monto_centimos": anterior + 1000}
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["precio"]["monto_centimos"] == anterior + 1000

    filas = db.scalars(
        select(ServicioPrecio)
        .where(ServicioPrecio.servicio_id == servicio_medio.id)
        .order_by(ServicioPrecio.id)
    ).all()
    assert len(filas) == 2, "el precio anterior se conserva como historia"
    assert filas[0].monto_centimos == anterior
    assert filas[0].vigente_hasta is not None, "la fila anterior queda cerrada"
    assert filas[1].vigente_hasta is None, "solo una vigencia queda abierta"


def test_un_cliente_no_puede_administrar_servicios(api_cliente):
    """RF-004 CA-01."""
    respuesta = api_cliente.get(f"{RUTA}/admin/servicios")

    assert respuesta.status_code == 403
    assert codigo_error(respuesta) == "PERMISO_DENEGADO"
