"""RF-023 - Registro de evidencias y observaciones.

Cubre:

* `CA-01` una fotografía cargada **la visualiza el cliente** en su servicio,
  con su marca de tiempo y su autor;
* `CA-02` / `4a` una carga fallida deja la evidencia **pendiente**, y el mismo
  envío repetido al recuperar la conexión **la sube sin intervención** y sin
  duplicar la fotografía;
* `3a` un archivo sobre el límite se **rechaza indicando el motivo** (la
  compresión es del cliente móvil; el backend dice por qué no lo acepta);
* el tope de **seis fotografías**, contado por momento del servicio;
* la precondición «el servicio se encuentra en curso», leída de
  `transicion_estado` y no de un nombre de estado (P3).
"""

import base64

from sqlalchemy import select

from app.models import Evidencia, MomentoEvidencia
from app.repositories import evento as evento_repo
from app.repositories import reserva as reserva_repo
from app.schemas import EvidenciaIn
from app.services import archivo_service, eventos, evidencia_service
from app.services.proveedores.almacenamiento import ClaveInvalida
from tests.conftest import (
    MIME_PNG,
    PIXEL_PNG_BASE64,
    RUTA,
    codigo_error,
    crear_reserva,
    entregar,
    instante,
    llevar_hasta_finalizado,
    pagar_en_caja,
    proximo_lunes,
)

PIXEL = base64.b64decode(PIXEL_PNG_BASE64)


class AlmacenamientoCaido:
    """Un almacén que siempre falla: la caída de red de `RF-023` `4a`.

    No usa la red (ningún simulado lo hace, sección 4 del plan): falla de
    forma determinista, que es justo lo que la prueba necesita.
    """

    def guardar(self, clave, contenido, mime="application/octet-stream"):
        raise ClaveInvalida("El almacén no está disponible.")

    def leer(self, clave):  # pragma: no cover - nunca llega a leerse
        raise ClaveInvalida("El almacén no está disponible.")

    def url(self, clave):  # pragma: no cover - nunca llega a pedirse
        return ""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _reserva_en_recepcion(api_cliente, api_recepcion, servicio, vehiculo_id, hora=10):
    """Una reserva con el vehículo ya recibido: el servicio está en curso."""
    reserva = crear_reserva(
        api_cliente, servicio.id, vehiculo_id, instante(proximo_lunes(), hora, 0)
    ).json()
    respuesta = api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": False}
    )
    assert respuesta.status_code == 200, respuesta.text
    return reserva


def _registrar(api, reserva_id, **cuerpo):
    cuerpo.setdefault("momento", MomentoEvidencia.ANTES.value)
    return api.post(f"{RUTA}/reservas/{reserva_id}/evidencias", json=cuerpo)


def _con_foto(api, reserva_id, **cuerpo):
    cuerpo.setdefault("contenido_base64", PIXEL_PNG_BASE64)
    cuerpo.setdefault("mime", MIME_PNG)
    return _registrar(api, reserva_id, **cuerpo)


# --------------------------------------------------------------------------
# CA-01 - el cliente ve la fotografía de su servicio
# --------------------------------------------------------------------------
def test_el_cliente_visualiza_la_fotografia_de_su_servicio(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id
):
    """`RF-023` `CA-01`: «cuando el cliente abre su servicio, puede visualizarla»."""
    reserva = _reserva_en_recepcion(api_cliente, api_recepcion, servicio_medio, vehiculo_id)

    creada = _con_foto(api_recepcion, reserva["id"], observacion="Rayón previo en el parachoques")
    assert creada.status_code == 201, creada.text

    listado = api_cliente.get(f"{RUTA}/reservas/{reserva['id']}/evidencias")

    assert listado.status_code == 200, listado.text
    items = listado.json()["items"]
    assert len(items) == 1
    foto = items[0]
    assert foto["momento"] == MomentoEvidencia.ANTES.value
    assert foto["estado_carga"] == "subida"
    assert foto["observacion"] == "Rayón previo en el parachoques"
    # RF-023 salida: «archivos asociados al servicio con marca de tiempo y autor».
    assert foto["registrada_en"] is not None
    assert foto["autor"], "la evidencia va firmada por quien la tomó"
    assert foto["url"], "y con dónde se descarga"

    descarga = api_cliente.get(foto["url"])
    assert descarga.status_code == 200
    assert descarga.content == PIXEL


def test_las_evidencias_de_otro_cliente_no_se_listan(
    api_cliente, api_recepcion, cliente_http, servicio_medio, vehiculo_id
):
    """Misma autorización horizontal que la reserva: 404, no 403 (RF-017 CA-03)."""
    reserva = _reserva_en_recepcion(api_cliente, api_recepcion, servicio_medio, vehiculo_id)
    _con_foto(api_recepcion, reserva["id"])

    cliente_http.post(
        f"{RUTA}/auth/registro",
        json={
            "nombres": "Beto",
            "apellidos": "Ruiz",
            "correo": "beto.evidencias@example.com",
            "telefono": "987111222",
            "tipo_documento": "dni",
            "numero_documento": "71111333",
            "password": "Aqua1234",
            "acepta_politica": True,
        },
    )
    token = cliente_http.post(
        f"{RUTA}/auth/login",
        json={"correo": "beto.evidencias@example.com", "password": "Aqua1234"},
    ).json()["access_token"]

    respuesta = cliente_http.get(
        f"{RUTA}/reservas/{reserva['id']}/evidencias",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert respuesta.status_code == 404
    assert codigo_error(respuesta) == "RECURSO_NO_ENCONTRADO"


def test_el_cliente_no_registra_evidencias(api_cliente, api_recepcion, servicio_medio, vehiculo_id):
    """`RF-023` actores: Operario y Recepcionista. El cliente solo mira."""
    reserva = _reserva_en_recepcion(api_cliente, api_recepcion, servicio_medio, vehiculo_id)

    respuesta = _con_foto(api_cliente, reserva["id"])

    assert respuesta.status_code == 403
    assert codigo_error(respuesta) == "PERMISO_DENEGADO"


def test_el_operario_registra_la_evidencia_del_despues(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """La mitad «después» se toma en la bahía, y la toma el operario."""
    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    ).json()
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])

    respuesta = _con_foto(api_operario, reserva["id"], momento=MomentoEvidencia.DESPUES.value)

    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["momento"] == MomentoEvidencia.DESPUES.value


# --------------------------------------------------------------------------
# 4a y CA-02 - pendiente con reintento, sin intervención
# --------------------------------------------------------------------------
def test_registrar_sin_contenido_deja_la_evidencia_pendiente(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id
):
    """`RF-023` `4a`: la evidencia existe antes que sus bytes."""
    reserva = _reserva_en_recepcion(api_cliente, api_recepcion, servicio_medio, vehiculo_id)

    respuesta = _registrar(api_recepcion, reserva["id"], referencia_cliente="foto-1")

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["estado_carga"] == "pendiente"
    assert cuerpo["url"] is None
    assert cuerpo["intentos"] == 0


def test_una_carga_fallida_queda_pendiente_con_su_causa(
    api_cliente, api_recepcion, db, servicio_medio, vehiculo_id, usuario_admin
):
    """`RF-023` `4a`: «fallo de carga -> evidencia pendiente con reintento».

    El almacén se inyecta caído: el fallo del proveedor **no** tira la fila ni
    revienta la petición, porque entonces no quedaría nada que reintentar.
    """
    reserva = _reserva_en_recepcion(api_cliente, api_recepcion, servicio_medio, vehiculo_id)

    modelo = reserva_repo.obtener_por_id(db, reserva["id"])
    evidencia, creada = evidencia_service.registrar(
        db,
        modelo,
        EvidenciaIn(
            momento=MomentoEvidencia.ANTES,
            contenido_base64=PIXEL_PNG_BASE64,
            mime=MIME_PNG,
            referencia_cliente="foto-offline",
        ),
        usuario_admin,
        almacenamiento=AlmacenamientoCaido(),
    )

    assert creada is True
    assert evidencia.estado_carga == "fallida"
    assert evidencia.objeto_key is None
    assert evidencia.intentos == 1
    assert evidencia.error, "la causa queda en la fila, para que el taller la lea"


def test_al_recuperar_la_conexion_la_evidencia_se_sube_sin_intervencion(
    api_cliente, api_recepcion, db, servicio_medio, vehiculo_id, usuario_admin
):
    """`RF-023` `CA-02`: el mismo envío, repetido, completa la MISMA fila."""
    reserva = _reserva_en_recepcion(api_cliente, api_recepcion, servicio_medio, vehiculo_id)

    modelo = reserva_repo.obtener_por_id(db, reserva["id"])
    entrada = EvidenciaIn(
        momento=MomentoEvidencia.ANTES,
        contenido_base64=PIXEL_PNG_BASE64,
        mime=MIME_PNG,
        referencia_cliente="foto-offline",
    )
    fallida, _ = evidencia_service.registrar(
        db, modelo, entrada, usuario_admin, almacenamiento=AlmacenamientoCaido()
    )
    assert fallida.estado_carga == "fallida"

    # El dispositivo recupera la conexión y reenvía exactamente lo mismo.
    reintento = _con_foto(api_recepcion, reserva["id"], referencia_cliente="foto-offline")

    assert reintento.status_code == 200, reintento.text
    cuerpo = reintento.json()
    assert cuerpo["id"] == fallida.id, "es la misma evidencia, no una séptima foto"
    assert cuerpo["estado_carga"] == "subida"
    assert cuerpo["url"]
    assert cuerpo["error"] is None
    assert cuerpo["intentos"] == 2

    filas = db.scalars(select(Evidencia).where(Evidencia.reserva_id == reserva["id"])).all()
    assert len(filas) == 1


def test_reintentar_una_evidencia_ya_subida_no_la_duplica(
    api_cliente, api_recepcion, db, servicio_medio, vehiculo_id
):
    """Un reintento que llega tarde no vuelve a subir nada."""
    reserva = _reserva_en_recepcion(api_cliente, api_recepcion, servicio_medio, vehiculo_id)
    primera = _con_foto(api_recepcion, reserva["id"], referencia_cliente="foto-1")
    assert primera.status_code == 201

    segunda = _con_foto(api_recepcion, reserva["id"], referencia_cliente="foto-1")

    assert segunda.status_code == 200
    assert segunda.json()["id"] == primera.json()["id"]
    assert segunda.json()["url"] == primera.json()["url"]
    filas = db.scalars(select(Evidencia).where(Evidencia.reserva_id == reserva["id"])).all()
    assert len(filas) == 1


# --------------------------------------------------------------------------
# 3a - el archivo sobre el límite se rechaza con su motivo
# --------------------------------------------------------------------------
def test_una_imagen_por_encima_del_limite_se_rechaza_sin_crear_la_fila(
    api_cliente, api_recepcion, db, servicio_medio, vehiculo_id
):
    """`RF-023` `3a`: «se comprime o se pide otra imagen».

    El rechazo es definitivo, no un fallo a reintentar: reenviar el mismo
    archivo fallaría igual, así que no se abre una evidencia pendiente.
    """
    reserva = _reserva_en_recepcion(api_cliente, api_recepcion, servicio_medio, vehiculo_id)
    demasiado = base64.b64encode(b"x" * (archivo_service.limite_bytes() + 1)).decode()

    respuesta = _registrar(
        api_recepcion,
        reserva["id"],
        contenido_base64=demasiado,
        mime=MIME_PNG,
        referencia_cliente="foto-grande",
    )

    assert respuesta.status_code == 422, respuesta.text
    assert codigo_error(respuesta) == "ARCHIVO_RECHAZADO"
    assert db.scalars(select(Evidencia).where(Evidencia.reserva_id == reserva["id"])).all() == []


# --------------------------------------------------------------------------
# Hasta seis fotografías, por momento
# --------------------------------------------------------------------------
def test_la_septima_fotografia_de_un_momento_se_rechaza(
    api_cliente, api_recepcion, servicio_medio, vehiculo_id
):
    """`RF-023`: «hasta SEIS fotografías», contadas por momento del servicio."""
    reserva = _reserva_en_recepcion(api_cliente, api_recepcion, servicio_medio, vehiculo_id)

    for indice in range(evidencia_service.MAXIMO_POR_MOMENTO):
        creada = _con_foto(api_recepcion, reserva["id"], referencia_cliente=f"antes-{indice}")
        assert creada.status_code == 201, creada.text

    sobrante = _con_foto(api_recepcion, reserva["id"], referencia_cliente="antes-6")

    assert sobrante.status_code == 422
    assert codigo_error(sobrante) == "LIMITE_DE_EVIDENCIAS"

    # El cupo del «después» es suyo: seis fotos del antes no dejan al cliente
    # sin la única mitad que el CA-01 le enseña.
    despues = _con_foto(
        api_recepcion,
        reserva["id"],
        momento=MomentoEvidencia.DESPUES.value,
        referencia_cliente="despues-0",
    )
    assert despues.status_code == 201, despues.text


# --------------------------------------------------------------------------
# Precondición: el servicio se encuentra en curso (P3, sin nombrar estados)
# --------------------------------------------------------------------------
def test_un_servicio_entregado_no_admite_mas_evidencias(
    api_cliente, api_recepcion, api_operario, db, servicio_medio, vehiculo_id
):
    """`RF-023` precondición. «En curso» = la tabla declara una salida (P3)."""
    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(), 10, 0)
    ).json()
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])
    assert pagar_en_caja(
        api_recepcion, reserva["id"], reserva["monto"]["monto_centimos"]
    ).status_code in (200, 201)
    assert entregar(api_recepcion, reserva["id"]).status_code == 200

    respuesta = _con_foto(api_recepcion, reserva["id"], momento=MomentoEvidencia.DESPUES.value)

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "SERVICIO_NO_EN_CURSO"


# --------------------------------------------------------------------------
# P7 - toda operación sensible deja su evento
# --------------------------------------------------------------------------
def test_registrar_una_evidencia_deja_su_evento_de_dominio(
    api_cliente, api_recepcion, db, servicio_medio, vehiculo_id
):
    reserva = _reserva_en_recepcion(api_cliente, api_recepcion, servicio_medio, vehiculo_id)
    _con_foto(api_recepcion, reserva["id"])

    registrado = evento_repo.obtener_ultimo(
        db, eventos.ENTIDAD_RESERVA, reserva["id"], eventos.EVIDENCIA_REGISTRADA
    )

    assert registrado is not None
    assert registrado.datos["estado_carga"] == "subida"
    assert registrado.datos["momento"] == MomentoEvidencia.ANTES.value
