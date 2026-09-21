"""Almacenamiento de objetos: subida y servido genéricos.

INC-4 trajo `ProveedorAlmacenamiento` para el comprobante de `RF-027` y dejó
una sola puerta, la del PDF. Estas pruebas cubren la puerta genérica que
faltaba y las dos columnas que la estaban esperando sin endpoint:

* `usuario.foto_perfil_key` (`RF-006`, la dejó INC-3 persistida);
* `servicio.imagen_url` (`RF-009`, la dejó INC-2 como texto libre).

Y el borde que hace que servir objetos por clave sea razonable: una clave que
intenta salirse del almacén responde lo mismo que una clave inexistente.
"""

import base64

import pytest

from app.config import settings
from app.core.errors import ArchivoRechazado, RecursoNoEncontrado
from app.services import archivo_service
from app.services.proveedores.almacenamiento import RUTA_PUBLICA
from tests.conftest import MIME_PNG, PIXEL_PNG_BASE64, RUTA, codigo_error

PIXEL = base64.b64decode(PIXEL_PNG_BASE64)


def _subir(api, *, carpeta: str = "perfiles", contenido: str = PIXEL_PNG_BASE64, mime=MIME_PNG):
    return api.post(
        f"{RUTA}/archivos",
        json={"contenido_base64": contenido, "mime": mime, "carpeta": carpeta},
    )


# --------------------------------------------------------------------------
# La puerta genérica
# --------------------------------------------------------------------------
def test_subir_una_imagen_devuelve_su_clave_y_su_url(api_cliente):
    """La clave es el contrato; la URL se deriva de ella (sección 4 del plan)."""
    respuesta = _subir(api_cliente)

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["clave"].startswith("perfiles/")
    assert cuerpo["clave"].endswith(".png")
    assert cuerpo["url"] == f"{RUTA_PUBLICA}/{cuerpo['clave']}"
    assert cuerpo["mime"] == MIME_PNG
    assert cuerpo["tamano_bytes"] == len(PIXEL)


def test_el_archivo_subido_se_descarga_por_su_url(api_cliente):
    """Lo que se guardó es exactamente lo que se sirve, con su tipo."""
    cuerpo = _subir(api_cliente).json()

    descarga = api_cliente.get(cuerpo["url"])

    assert descarga.status_code == 200, descarga.text
    assert descarga.content == PIXEL
    assert descarga.headers["content-type"].startswith(MIME_PNG)


def test_dos_subidas_no_comparten_clave(api_cliente):
    """La clave lleva un nombre aleatorio: nadie la adivina ni la pisa."""
    primera = _subir(api_cliente).json()["clave"]
    segunda = _subir(api_cliente).json()["clave"]

    assert primera != segunda


def test_descargar_un_archivo_exige_sesion(api_cliente, cliente_http):
    cuerpo = _subir(api_cliente).json()

    assert cliente_http.get(cuerpo["url"]).status_code == 401


# --------------------------------------------------------------------------
# RF-006 - la foto de perfil que INC-3 dejó sin endpoint
# --------------------------------------------------------------------------
def test_la_foto_de_perfil_se_guarda_como_clave_y_se_sirve(api_cliente):
    """`RF-006`: «foto en el almacenamiento simulado».

    INC-3 persistió `usuario.foto_perfil_key` y anotó que el endpoint llegaba
    con INC-6. Este es el circuito completo: subir, guardar la clave en el
    perfil y volver a leer la imagen.
    """
    clave = _subir(api_cliente, carpeta="perfiles").json()["clave"]

    perfil = api_cliente.patch(f"{RUTA}/perfil", json={"foto_perfil_key": clave})
    assert perfil.status_code == 200, perfil.text
    assert perfil.json()["usuario"]["foto_perfil_key"] == clave

    descarga = api_cliente.get(f"{RUTA_PUBLICA}/{clave}")
    assert descarga.status_code == 200
    assert descarga.content == PIXEL


# --------------------------------------------------------------------------
# RF-009 - la imagen del servicio que INC-2 dejó como texto libre
# --------------------------------------------------------------------------
def test_la_imagen_del_servicio_se_sirve_por_el_mismo_endpoint(api_admin, servicio_corto):
    """`RF-009` v1.0: «imagen referencial» del catálogo, ya servida de verdad."""
    cuerpo = _subir(api_admin, carpeta="servicios").json()

    actualizado = api_admin.patch(
        f"{RUTA}/admin/servicios/{servicio_corto.id}", json={"imagen_url": cuerpo["url"]}
    )
    assert actualizado.status_code == 200, actualizado.text
    assert actualizado.json()["imagen_url"] == cuerpo["url"]

    catalogo = api_admin.get(f"{RUTA}/servicios/{servicio_corto.id}")
    assert catalogo.json()["imagen_url"] == cuerpo["url"]
    assert api_admin.get(cuerpo["url"]).content == PIXEL


# --------------------------------------------------------------------------
# RF-023 `3a` en su forma general: se rechaza con el motivo
# --------------------------------------------------------------------------
def test_el_limite_de_tamano_es_un_megabyte(api_cliente):
    """`RNF-004` M4: «máx. 1 MB». El valor por defecto arranca sin `.env`."""
    assert settings.archivo_tamano_maximo_kb == 1024
    assert archivo_service.limite_bytes() == 1024 * 1024


def test_un_archivo_por_encima_del_limite_se_rechaza_con_su_motivo(api_cliente):
    """`RF-023` `3a`: el backend rechaza y dice cuánto pesa y cuál es el tope."""
    demasiado = base64.b64encode(b"x" * (archivo_service.limite_bytes() + 1)).decode()

    respuesta = _subir(api_cliente, contenido=demasiado)

    assert respuesta.status_code == 422, respuesta.text
    assert codigo_error(respuesta) == "ARCHIVO_RECHAZADO"
    campos = {d["campo"] for d in respuesta.json()["error"]["detalles"]}
    assert "limite_bytes" in campos


def test_un_tipo_que_no_es_imagen_se_rechaza_indicando_los_aceptados(api_cliente):
    respuesta = _subir(api_cliente, mime="application/zip")

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "ARCHIVO_RECHAZADO"
    mensajes = " ".join(d["mensaje"] for d in respuesta.json()["error"]["detalles"])
    assert "image/png" in mensajes


def test_un_contenido_que_no_es_base64_se_rechaza(api_cliente):
    respuesta = _subir(api_cliente, contenido="esto no es base64 ***")

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "ARCHIVO_RECHAZADO"


# --------------------------------------------------------------------------
# El almacén no se sale de su directorio
# --------------------------------------------------------------------------
@pytest.mark.parametrize("clave", ["../../etc/passwd", "/etc/passwd", "perfiles/../../secreto"])
def test_una_clave_que_se_sale_del_almacen_responde_como_una_inexistente(clave):
    """Una clave inválida y una clave vacía responden igual: nada que sondear."""
    with pytest.raises(RecursoNoEncontrado):
        archivo_service.leer(clave)


def test_la_clave_generada_no_depende_del_nombre_que_envie_el_cliente():
    """La extensión sale del tipo declarado, no de lo que diga el cliente."""
    clave = archivo_service.clave_nueva("perfiles", "image/jpeg")

    assert clave.startswith("perfiles/")
    assert clave.endswith(".jpg")
    assert archivo_service.mime_de_clave(clave) == "image/jpeg"


def test_un_tipo_no_admitido_no_llega_a_construir_una_clave():
    with pytest.raises(ArchivoRechazado):
        archivo_service.normalizar_mime("text/html")
