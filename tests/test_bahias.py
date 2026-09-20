"""Bay administration (the v1.0 gap the MVP left open).

The MVP seeded four bays and offered no way to add, rename or retire one, which
RF-018 and RF-020 both need. The rules are RE-07 (four physical bays) and
RN-03: a bay still holding work does not disappear from under its bookings.
"""

from sqlalchemy import select

from app.models import MAXIMO_BAHIAS, Bahia, EventoDominio
from tests.conftest import (
    RUTA,
    codigo_error,
    crear_reserva,
    instante,
    proximo_lunes,
)


def _bahias(db) -> list[Bahia]:
    return list(db.scalars(select(Bahia).order_by(Bahia.id)).all())


def test_el_catalogo_muestra_las_cuatro_bahias_sembradas(api_admin):
    respuesta = api_admin.get(f"{RUTA}/admin/bahias")

    assert respuesta.status_code == 200, respuesta.text
    items = respuesta.json()["items"]
    assert len(items) == MAXIMO_BAHIAS
    assert all(item["activa"] for item in items)
    assert all(item["estado"] == "libre" for item in items)


def test_crear_una_bahia_mas_alla_del_limite_fisico_se_rechaza(api_admin):
    """RE-07: el local tiene cuatro bahías, no las que se quieran inventar."""
    respuesta = api_admin.post(f"{RUTA}/admin/bahias", json={"nombre": "Bahía 5"})

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "LIMITE_DE_BAHIAS"


def test_se_puede_registrar_una_bahia_inactiva_y_activarla_tras_liberar_sitio(api_admin, db):
    alta = api_admin.post(f"{RUTA}/admin/bahias", json={"nombre": "Bahía 5", "activa": False})

    assert alta.status_code == 201, alta.text
    nueva = alta.json()
    assert nueva["activa"] is False

    # Sin hacer sitio, activarla sigue chocando con RE-07.
    assert (
        api_admin.patch(f"{RUTA}/admin/bahias/{nueva['id']}", json={"activa": True}).status_code
        == 422
    )

    cuarta = _bahias(db)[3]
    assert (
        api_admin.patch(f"{RUTA}/admin/bahias/{cuarta.id}", json={"activa": False}).status_code
        == 200
    )
    respuesta = api_admin.patch(f"{RUTA}/admin/bahias/{nueva['id']}", json={"activa": True})

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["activa"] is True


def test_el_nombre_repetido_se_rechaza(api_admin):
    respuesta = api_admin.post(f"{RUTA}/admin/bahias", json={"nombre": "Bahía 1"})

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "NOMBRE_DE_BAHIA_DUPLICADO"


def test_renombrar_una_bahia_deja_bitacora(api_admin, db):
    bahia = _bahias(db)[0]

    respuesta = api_admin.patch(f"{RUTA}/admin/bahias/{bahia.id}", json={"nombre": "Túnel 1"})

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["nombre"] == "Túnel 1"
    evento = db.scalars(
        select(EventoDominio).where(EventoDominio.accion == "bahia.actualizada")
    ).first()
    assert evento.datos["nombre"]["anterior"] == "Bahía 1"


def test_no_se_desactiva_una_bahia_con_reservas_activas(
    api_cliente, api_admin, servicio_corto, vehiculo_id
):
    """RN-03: la reserva prometió un sitio y ese sitio no puede evaporarse."""
    reserva = crear_reserva(
        api_cliente, servicio_corto.id, vehiculo_id, instante(proximo_lunes(dias_minimos=2), 10, 0)
    ).json()

    respuesta = api_admin.patch(
        f"{RUTA}/admin/bahias/{reserva['bahia']['id']}", json={"activa": False}
    )

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "BAHIA_CON_RESERVAS"


def test_tras_cancelar_la_bahia_se_desactiva(api_cliente, api_admin, servicio_corto, vehiculo_id):
    reserva = crear_reserva(
        api_cliente, servicio_corto.id, vehiculo_id, instante(proximo_lunes(dias_minimos=2), 10, 0)
    ).json()
    api_cliente.post(
        f"{RUTA}/reservas/{reserva['id']}/cancelacion", json={"motivo": "Cierre de bahía"}
    )

    respuesta = api_admin.patch(
        f"{RUTA}/admin/bahias/{reserva['bahia']['id']}", json={"activa": False}
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["activa"] is False


def test_liberar_una_bahia_a_mano_es_posible(api_admin, db):
    """La salida de emergencia: un coche que se fue sin check-out."""
    bahia = _bahias(db)[0]
    bahia.estado = "ocupada"
    db.commit()

    respuesta = api_admin.patch(f"{RUTA}/admin/bahias/{bahia.id}", json={"estado": "libre"})

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "libre"


def test_la_administracion_de_bahias_exige_su_permiso(
    api_cliente, api_recepcion, api_operario, cliente_http
):
    assert cliente_http.get(f"{RUTA}/admin/bahias").status_code == 401
    assert api_cliente.get(f"{RUTA}/admin/bahias").status_code == 403
    assert api_recepcion.get(f"{RUTA}/admin/bahias").status_code == 403
    assert api_operario.get(f"{RUTA}/admin/bahias").status_code == 403


def test_editar_una_bahia_inexistente_responde_404(api_admin):
    assert api_admin.patch(f"{RUTA}/admin/bahias/9999", json={"nombre": "X"}).status_code == 404
