"""RF-027 - Generación del comprobante electrónico.

Cubre:

* `CA-01` un pago confirmado emite un comprobante con **el desglose y el
  número correlativo**;
* `CA-02` el comprobante emitido **se descarga en PDF** desde el historial;
* `3a` si falla el envío del correo, **el comprobante sigue disponible en la
  aplicación**.

El PDF es real y está escrito a mano (sección 4 del plan): ninguna dependencia
nueva, y la prueba lo verifica por su cabecera, su tabla de referencias
cruzadas y su texto, no por la extensión del fichero.
"""

from sqlalchemy import select

from app.config import settings
from app.models import (
    CanalNotificacion,
    Comprobante,
    EstadoEnvio,
    EventoNotificacion,
    Notificacion,
    Reserva,
)
from app.schemas import PagoCrear
from app.services import comprobante_service, pago_service
from app.services.proveedores.almacenamiento import proveedor_almacenamiento
from app.services.proveedores.correo import CorreoSimulado
from app.services.proveedores.documentos import CODIFICACION, MIME_PDF
from tests.conftest import (
    RUTA,
    codigo_error,
    crear_reserva,
    instante,
    llevar_hasta_finalizado,
    pagar_en_linea,
    proximo_lunes,
)

MONTO_ESPERADO = 2500


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _reserva_finalizada(
    api_cliente, api_recepcion, api_operario, db, servicio, vehiculo_id, hora=10
):
    reserva = crear_reserva(
        api_cliente, servicio.id, vehiculo_id, instante(proximo_lunes(), hora, 0)
    ).json()
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])
    return reserva


def _pagar_en_caja(api_recepcion, reserva_id, *, monto=MONTO_ESPERADO, clave="comprobante-1"):
    return api_recepcion.post(
        f"{RUTA}/reservas/{reserva_id}/pagos",
        json={"medio": "efectivo", "monto_centimos": monto},
        headers={"Idempotency-Key": clave},
    )


def _texto_del_pdf(contenido: bytes) -> str:
    return contenido.decode(CODIFICACION, errors="replace")


# --------------------------------------------------------------------------
# CA-01 - desglose y número correlativo
# --------------------------------------------------------------------------
def test_el_pago_confirmado_emite_el_comprobante_con_su_numero_correlativo(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-027 `CA-01`: «contiene el desglose y el número correlativo»."""
    reserva = _reserva_finalizada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    assert _pagar_en_caja(api_recepcion, reserva["id"]).status_code == 201

    respuesta = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}/comprobante")

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["serie"] == settings.comprobante_serie
    assert cuerpo["numero_correlativo"] == 1
    assert cuerpo["numero"] == f"{settings.comprobante_serie}-00000001"
    assert cuerpo["monto"] == {"monto_centimos": MONTO_ESPERADO, "moneda": "PEN"}
    assert cuerpo["medio_pago"] == "efectivo"
    assert cuerpo["estado"] == "emitido"
    assert cuerpo["archivo_url"].endswith(f"/comprobantes/{cuerpo['id']}/archivo")

    # El detalle de la reserva también lo enlaza (RF-017 delta v1.0).
    detalle = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}").json()
    assert detalle["comprobante"]["numero"] == cuerpo["numero"]


def test_el_pdf_lleva_el_desglose_congelado_de_la_reserva(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-027 `CA-01`: el desglose del PDF es el de INC-2, no el catálogo de hoy."""
    reserva = _reserva_finalizada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    _pagar_en_caja(api_recepcion, reserva["id"])
    fila = db.scalars(select(Comprobante)).one()

    texto = _texto_del_pdf(comprobante_service.archivo(fila))

    assert f"Comprobante: {fila.numero}" in texto
    assert f"Reserva: {reserva['codigo']}" in texto
    assert "Precio base: PEN 25.00" in texto
    assert "Factor sedan: x1.000" in texto
    assert "TOTAL: PEN 25.00" in texto
    assert "Medio de pago: efectivo" in texto
    assert "IGV incluido" in texto, "RN-12"


def test_la_numeracion_es_correlativa_entre_comprobantes(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-027 `CA-01`: una serie es una serie, sin huecos ni repeticiones."""
    primera = _reserva_finalizada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id, hora=10
    )
    _pagar_en_caja(api_recepcion, primera["id"], clave="correlativo-1")

    segunda = _reserva_finalizada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id, hora=12
    )
    _pagar_en_caja(api_recepcion, segunda["id"], clave="correlativo-2")

    numeros = [
        fila.numero_correlativo
        for fila in db.scalars(select(Comprobante).order_by(Comprobante.id)).all()
    ]
    assert numeros == [1, 2]


# --------------------------------------------------------------------------
# CA-02 - se descarga en PDF
# --------------------------------------------------------------------------
def test_el_comprobante_se_descarga_en_pdf(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """RF-027 `CA-02`: «entonces puede descargarse en PDF».

    El fichero es un PDF 1.4 de verdad: cabecera, tabla ``xref`` cuyo
    desplazamiento apunta donde dice ``startxref``, y ``%%EOF`` al final.
    """
    reserva = _reserva_finalizada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    _pagar_en_caja(api_recepcion, reserva["id"])
    comprobante = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}/comprobante").json()

    respuesta = api_cliente.get(f"{RUTA}/comprobantes/{comprobante['id']}/archivo")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.headers["content-type"] == MIME_PDF
    assert comprobante["numero"] in respuesta.headers["content-disposition"]

    contenido = respuesta.content
    assert contenido.startswith(b"%PDF-1.4")
    assert contenido.rstrip().endswith(b"%%EOF")
    inicio_xref = int(contenido.rsplit(b"startxref", 1)[1].split()[0])
    assert contenido[inicio_xref : inicio_xref + 4] == b"xref", "la tabla está donde dice"


def test_el_pdf_se_guarda_en_el_almacenamiento_simulado(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """Sección 4 del plan: el fichero vive en el almacén, bajo una CLAVE."""
    reserva = _reserva_finalizada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    _pagar_en_caja(api_recepcion, reserva["id"])
    fila = db.scalars(select(Comprobante)).one()

    assert fila.archivo_key == f"comprobantes/{fila.serie}/{fila.numero}.pdf"
    assert not fila.archivo_key.startswith("/"), "es una clave, no una ruta"
    almacen = proveedor_almacenamiento()
    assert almacen.leer(fila.archivo_key).startswith(b"%PDF-1.4")
    assert almacen.url(fila.archivo_key).endswith(fila.archivo_key)


def test_el_comprobante_del_cobro_en_linea_se_emite_igual(
    api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-027 precondición: «el pago se encuentra confirmado», venga de donde venga."""
    reserva = crear_reserva(
        api_cliente,
        servicio_medio.id,
        vehiculo_id,
        instante(proximo_lunes(), 10, 0),
        modalidad_pago="en_linea",
    ).json()
    assert pagar_en_linea(api_cliente, reserva["id"], clave="con-comprobante").status_code == 200

    fila = db.scalars(select(Comprobante)).one()
    texto = _texto_del_pdf(comprobante_service.archivo(fila))
    assert "Medio de pago: tarjeta" in texto
    assert "Modalidad: en_linea" in texto
    assert "Referencia de la pasarela: SIM-COB-" in texto


# --------------------------------------------------------------------------
# 3a - el correo falla y el comprobante sigue disponible
# --------------------------------------------------------------------------
def test_si_falla_el_correo_el_comprobante_sigue_disponible_en_la_aplicacion(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id, usuario_admin
):
    """RF-027 `3a`, el flujo alterno que el requisito subraya.

    El proveedor de correo se inyecta rompido: el despacho falla, queda su
    causa registrada (RF-029 `CA-02`) y **el PDF se sigue descargando**.
    """
    reserva_json = _reserva_finalizada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    reserva = db.get(Reserva, reserva_json["id"])

    pago_service.registrar(
        db,
        reserva,
        PagoCrear(medio="efectivo", monto_centimos=MONTO_ESPERADO),
        usuario_admin,
        "correo-caido-1",
        correo=lambda **enlace: CorreoSimulado(fallar=True, **enlace),
        espera=lambda _segundos: None,
    )

    envio = db.scalars(
        select(Notificacion).where(
            Notificacion.reserva_id == reserva.id,
            Notificacion.evento == EventoNotificacion.COMPROBANTE.value,
            Notificacion.canal == CanalNotificacion.CORREO.value,
        )
    ).one()
    assert envio.estado_envio == EstadoEnvio.FALLIDA.value
    assert envio.error, "RF-029 CA-02: la causa queda escrita"

    # Y aun así el comprobante existe y se descarga: es lo que pide el flujo.
    comprobante = api_cliente.get(f"{RUTA}/reservas/{reserva.id}/comprobante")
    assert comprobante.status_code == 200, comprobante.text
    archivo = api_cliente.get(f"{RUTA}/comprobantes/{comprobante.json()['id']}/archivo")
    assert archivo.status_code == 200
    assert archivo.content.startswith(b"%PDF-1.4")


# --------------------------------------------------------------------------
# Bordes
# --------------------------------------------------------------------------
def test_sin_pago_confirmado_no_hay_comprobante(api_cliente, servicio_medio, vehiculo_id):
    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    ).json()

    respuesta = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}/comprobante")

    assert respuesta.status_code == 404
    assert codigo_error(respuesta) == "COMPROBANTE_NO_DISPONIBLE"


def test_un_cliente_no_descarga_el_comprobante_de_otro(
    api_cliente, api_recepcion, api_operario, cliente_http, db, servicio_medio, vehiculo_id
):
    """Autorización horizontal (RF-017 `CA-03`): el comprobante sigue a su reserva."""
    reserva = _reserva_finalizada(
        api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
    )
    _pagar_en_caja(api_recepcion, reserva["id"])
    comprobante = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}/comprobante").json()

    otro = cliente_http.post(
        f"{RUTA}/auth/registro",
        json={
            "nombres": "Otra",
            "apellidos": "Clienta",
            "correo": "otra.clienta@aqualav.pe",
            "telefono": "987111222",
            "password": "Clienta1234",
            "tipo_documento": "dni",
            "numero_documento": "41000099",
            "acepta_politica": True,
        },
    )
    assert otro.status_code == 201, otro.text
    token = cliente_http.post(
        f"{RUTA}/auth/login",
        json={"correo": "otra.clienta@aqualav.pe", "password": "Clienta1234"},
    ).json()["access_token"]
    cliente_http.headers.update({"Authorization": f"Bearer {token}"})

    respuesta = cliente_http.get(f"{RUTA}/comprobantes/{comprobante['id']}/archivo")

    assert respuesta.status_code == 404, "mismo 404 que pedir la reserva ajena"
