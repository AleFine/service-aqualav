"""Payment registration (RF-026)."""

from sqlalchemy import func, select

from app.models import Pago
from tests.conftest import RUTA, codigo_error, crear_reserva, instante, proximo_lunes


def _reserva_finalizada(api_cliente, api_personal, servicio, vehiculo_id) -> dict:
    """A reservation taken all the way to ``finalizado``, ready to be charged."""
    reserva = crear_reserva(
        api_cliente, servicio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    ).json()
    api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": False}
    )
    api_personal.post(f"{RUTA}/reservas/{reserva['id']}/estado", json={"estado": "finalizado"})
    return reserva


def _pagar(api_personal, reserva_id, *, monto=2500, clave="pago-001", motivo=None):
    cuerpo = {"medio": "efectivo", "monto_centimos": monto}
    if motivo is not None:
        cuerpo["motivo_diferencia"] = motivo
    return api_personal.post(
        f"{RUTA}/reservas/{reserva_id}/pagos",
        json=cuerpo,
        headers={"Idempotency-Key": clave},
    )


def test_registrar_el_pago_lo_deja_confirmado(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """RF-026 CA-01 y CA-03: medio, monto, autor y fecha quedan registrados."""
    reserva = _reserva_finalizada(api_cliente, api_personal, servicio_medio, vehiculo_id)

    respuesta = _pagar(api_personal, reserva["id"])

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "confirmado"
    assert cuerpo["medio"] == "efectivo"
    assert cuerpo["monto"] == {"monto_centimos": 2500, "moneda": "PEN"}
    assert cuerpo["autor"]
    assert cuerpo["registrado_en"]

    detalle = api_personal.get(f"{RUTA}/reservas/{reserva['id']}").json()
    assert detalle["pago"]["id"] == cuerpo["id"]


def test_sin_cabecera_de_idempotencia_responde_400(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """EXTENSION POINT P6: la clave es obligatoria desde el MVP."""
    reserva = _reserva_finalizada(api_cliente, api_personal, servicio_medio, vehiculo_id)

    respuesta = api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/pagos",
        json={"medio": "efectivo", "monto_centimos": 2500},
    )

    assert respuesta.status_code == 400
    assert codigo_error(respuesta) == "IDEMPOTENCY_KEY_REQUERIDA"


def test_repetir_la_clave_devuelve_200_y_un_unico_pago(
    api_cliente, api_personal, db, servicio_medio, vehiculo_id
):
    """RF-026 CA-02 y flujo 4a."""
    reserva = _reserva_finalizada(api_cliente, api_personal, servicio_medio, vehiculo_id)

    primero = _pagar(api_personal, reserva["id"], clave="reintento-1")
    segundo = _pagar(api_personal, reserva["id"], clave="reintento-1")

    assert primero.status_code == 201, primero.text
    assert segundo.status_code == 200, segundo.text
    assert segundo.json()["id"] == primero.json()["id"]

    total = db.scalar(select(func.count(Pago.id)).where(Pago.reserva_id == reserva["id"]))
    assert total == 1


def test_la_clave_tambien_se_acepta_en_el_cuerpo(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """El contrato admite ``idempotency_key`` como campo del cuerpo."""
    reserva = _reserva_finalizada(api_cliente, api_personal, servicio_medio, vehiculo_id)

    respuesta = api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/pagos",
        json={"medio": "tarjeta_pos", "monto_centimos": 2500, "idempotency_key": "en-el-cuerpo"},
    )

    assert respuesta.status_code == 201, respuesta.text


def test_un_monto_distinto_exige_motivo(api_cliente, api_personal, servicio_medio, vehiculo_id):
    """RF-026 flujo 3a."""
    reserva = _reserva_finalizada(api_cliente, api_personal, servicio_medio, vehiculo_id)

    sin_motivo = _pagar(api_personal, reserva["id"], monto=2000, clave="dif-1")
    assert sin_motivo.status_code == 422
    assert codigo_error(sin_motivo) == "DATOS_INVALIDOS"

    con_motivo = _pagar(
        api_personal,
        reserva["id"],
        monto=2000,
        clave="dif-2",
        motivo="Descuento autorizado por el administrador",
    )
    assert con_motivo.status_code == 201, con_motivo.text
    assert con_motivo.json()["monto"]["monto_centimos"] == 2000


def test_no_se_cobra_una_reserva_que_no_esta_finalizada(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """El contrato solo permite cobrar cuando el estado es ``finalizado``."""
    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    ).json()

    respuesta = _pagar(api_personal, reserva["id"], clave="temprano")

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "DATOS_INVALIDOS"


def test_un_cliente_no_puede_registrar_pagos(
    api_cliente, api_personal, servicio_medio, vehiculo_id
):
    """``pago:registrar`` es del personal (RF-004 CA-01)."""
    reserva = _reserva_finalizada(api_cliente, api_personal, servicio_medio, vehiculo_id)

    respuesta = _pagar(api_cliente, reserva["id"], clave="del-cliente")

    assert respuesta.status_code == 403


def test_cobro_permitido_en_estado_posterior_a_finalizado(
    api_cliente, api_personal, db, servicio_medio, vehiculo_id
):
    """C4: cobrar depende de que el servicio haya terminado, no del nombre del
    estado. Con ``finalizado -> en_revision`` declarado como dato, el cobro
    sigue aceptándose en el estado nuevo, sin tocar código.
    """
    from app.models import TransicionEstado

    reserva = _reserva_finalizada(api_cliente, api_personal, servicio_medio, vehiculo_id)

    db.add(
        TransicionEstado(
            estado_origen="finalizado",
            estado_destino="en_revision",
            permiso_requerido="reserva:avanzar_estado",
            endpoint=None,
            marca_fin_servicio=False,
        )
    )
    db.commit()

    avance = api_personal.post(
        f"{RUTA}/reservas/{reserva['id']}/estado", json={"estado": "en_revision"}
    )
    assert avance.status_code == 200, avance.text
    assert avance.json()["estado"] == "en_revision"

    respuesta = _pagar(api_personal, reserva["id"], clave="tras-revision")

    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["estado"] == "confirmado"
