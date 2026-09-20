"""Password recovery, sessions and profile (RF-003, RF-005, RF-006).

The three requirements the MVP could only promise on paper, because a refresh
token was a self-contained JWT with nowhere to write down that it had been
retired. ``token_refresco`` is that place, and it is what lets RF-003 close
every previous session and RF-005 answer 401 to a token that was signed out.
"""

import re
from datetime import timedelta

from sqlalchemy import select

from app.config import settings
from app.core.horario import ahora_utc
from app.models import TokenRecuperacion, Usuario
from app.services.proveedores.correo import PROVEEDOR_CORREO_PREDETERMINADO
from tests.conftest import RUTA, codigo_error

NUEVO = {
    "nombres": "Beto",
    "apellidos": "Ruiz",
    "correo": "beto.ruiz@example.com",
    "telefono": "987111222",
    "tipo_documento": "dni",
    "numero_documento": "73334455",
    "password": "Aqua1234",
    "acepta_politica": True,
}
NUEVA_PASSWORD = "OtraClave9"


def _token_del_correo(destino: str) -> str:
    mensajes = [m for m in PROVEEDOR_CORREO_PREDETERMINADO.enviados if m.destino == destino]
    assert mensajes, f"el proveedor simulado debería tener un mensaje para {destino}"
    encontrado = re.search(r"token=(\S+)", mensajes[-1].cuerpo)
    assert encontrado, mensajes[-1].cuerpo
    return encontrado.group(1)


def _registrar_verificado(cliente_http, db) -> str:
    """Register the test account and verify it, so it starts clean."""
    PROVEEDOR_CORREO_PREDETERMINADO.enviados.clear()
    assert cliente_http.post(f"{RUTA}/auth/registro", json=NUEVO).status_code == 201
    token = _token_del_correo(NUEVO["correo"])
    assert cliente_http.post(f"{RUTA}/auth/verificacion", json={"token": token}).status_code == 200
    return NUEVO["correo"]


def _login(cliente_http, correo: str, password: str):
    return cliente_http.post(f"{RUTA}/auth/login", json={"correo": correo, "password": password})


def _pedir_recuperacion(cliente_http, correo: str) -> str:
    PROVEEDOR_CORREO_PREDETERMINADO.enviados.clear()
    respuesta = cliente_http.post(f"{RUTA}/auth/password/recuperacion", json={"correo": correo})
    assert respuesta.status_code == 200, respuesta.text
    return _token_del_correo(correo)


# --------------------------------------------------------------------------
# RF-003 - password recovery
# --------------------------------------------------------------------------
def test_la_respuesta_es_identica_exista_o_no_la_cuenta(cliente_http):
    """RF-003 flujo 2a: no enumeración de usuarios.

    Ni el cuerpo ni el código distinguen una dirección registrada de una que
    no lo está: cualquier diferencia sería la fuga que el requisito prohíbe.
    """
    existente = cliente_http.post(
        f"{RUTA}/auth/password/recuperacion", json={"correo": settings.seed_cliente_correo}
    )
    inexistente = cliente_http.post(
        f"{RUTA}/auth/password/recuperacion", json={"correo": "nadie@example.com"}
    )

    assert existente.status_code == inexistente.status_code == 200
    assert existente.json() == inexistente.json()


def test_un_token_vigente_cambia_la_contrasena_y_queda_inutilizable(cliente_http, db):
    """RF-003 CA-01: se actualiza la contraseña y el token ya no sirve."""
    correo = _registrar_verificado(cliente_http, db)
    token = _pedir_recuperacion(cliente_http, correo)

    respuesta = cliente_http.post(
        f"{RUTA}/auth/password/restablecer",
        json={
            "token": token,
            "password": NUEVA_PASSWORD,
            "password_confirmacion": NUEVA_PASSWORD,
        },
    )

    assert respuesta.status_code == 200, respuesta.text
    assert _login(cliente_http, correo, NUEVA_PASSWORD).status_code == 200
    assert _login(cliente_http, correo, NUEVO["password"]).status_code == 401

    segundo_uso = cliente_http.post(
        f"{RUTA}/auth/password/restablecer",
        json={
            "token": token,
            "password": "TerceraClave9",
            "password_confirmacion": "TerceraClave9",
        },
    )
    assert segundo_uso.status_code == 400
    assert codigo_error(segundo_uso) == "TOKEN_INVALIDO"


def test_un_token_de_mas_de_treinta_minutos_responde_400(cliente_http, db):
    """RF-003 CA-02: pasados los 30 minutos el enlace deja de valer."""
    correo = _registrar_verificado(cliente_http, db)
    token = _pedir_recuperacion(cliente_http, correo)

    fila = db.scalars(select(TokenRecuperacion).order_by(TokenRecuperacion.id.desc())).first()
    fila.expira_en = ahora_utc() - timedelta(minutes=1)
    db.commit()

    respuesta = cliente_http.post(
        f"{RUTA}/auth/password/restablecer",
        json={
            "token": token,
            "password": NUEVA_PASSWORD,
            "password_confirmacion": NUEVA_PASSWORD,
        },
    )

    assert respuesta.status_code == 400
    assert codigo_error(respuesta) == "TOKEN_INVALIDO"
    assert _login(cliente_http, correo, NUEVO["password"]).status_code == 200


def test_la_doble_captura_debe_coincidir(cliente_http, db):
    """RF-003 entradas: «nueva contraseña (doble captura)»."""
    correo = _registrar_verificado(cliente_http, db)
    token = _pedir_recuperacion(cliente_http, correo)

    respuesta = cliente_http.post(
        f"{RUTA}/auth/password/restablecer",
        json={"token": token, "password": NUEVA_PASSWORD, "password_confirmacion": "OtraCosa9"},
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "VALIDACION"


def test_cambiar_la_contrasena_invalida_las_sesiones_previas(cliente_http, db):
    """RF-003: «al cambiar la contraseña se invalidan todas las sesiones»."""
    correo = _registrar_verificado(cliente_http, db)
    sesion = _login(cliente_http, correo, NUEVO["password"]).json()
    token = _pedir_recuperacion(cliente_http, correo)

    cliente_http.post(
        f"{RUTA}/auth/password/restablecer",
        json={
            "token": token,
            "password": NUEVA_PASSWORD,
            "password_confirmacion": NUEVA_PASSWORD,
        },
    )

    respuesta = cliente_http.post(
        f"{RUTA}/auth/refresh", json={"refresh_token": sesion["refresh_token"]}
    )
    assert respuesta.status_code == 401
    assert codigo_error(respuesta) == "NO_AUTENTICADO"


# --------------------------------------------------------------------------
# RF-005 - sign out and credential expiry
# --------------------------------------------------------------------------
def test_reutilizar_el_token_tras_cerrar_sesion_responde_401(cliente_http):
    """RF-005 CA-01."""
    sesion = _login(
        cliente_http, settings.seed_cliente_correo, settings.seed_cliente_password
    ).json()

    salida = cliente_http.post(
        f"{RUTA}/auth/logout", json={"refresh_token": sesion["refresh_token"]}
    )

    assert salida.status_code == 200, salida.text
    respuesta = cliente_http.post(
        f"{RUTA}/auth/refresh", json={"refresh_token": sesion["refresh_token"]}
    )
    assert respuesta.status_code == 401
    assert codigo_error(respuesta) == "NO_AUTENTICADO"


def test_el_logout_es_idempotente_y_no_delata_al_token(cliente_http):
    """Flujo 2a: la app encola la revocación; repetirla no puede ser un error."""
    sesion = _login(
        cliente_http, settings.seed_cliente_correo, settings.seed_cliente_password
    ).json()
    cuerpo = {"refresh_token": sesion["refresh_token"]}

    primero = cliente_http.post(f"{RUTA}/auth/logout", json=cuerpo)
    segundo = cliente_http.post(f"{RUTA}/auth/logout", json=cuerpo)
    inventado = cliente_http.post(f"{RUTA}/auth/logout", json={"refresh_token": "no-es-un-token"})

    assert primero.status_code == segundo.status_code == inventado.status_code == 200
    assert primero.json() == segundo.json() == inventado.json()


def test_renovar_el_acceso_no_interrumpe_al_usuario(cliente_http):
    """RF-005 CA-02: el token de acceso se renueva sin intervención."""
    sesion = _login(
        cliente_http, settings.seed_cliente_correo, settings.seed_cliente_password
    ).json()

    renovada = cliente_http.post(
        f"{RUTA}/auth/refresh", json={"refresh_token": sesion["refresh_token"]}
    )

    assert renovada.status_code == 200, renovada.text
    nuevo = renovada.json()["access_token"]
    recurso = cliente_http.get(f"{RUTA}/vehiculos", headers={"Authorization": f"Bearer {nuevo}"})
    assert recurso.status_code == 200, recurso.text


def test_cerrar_una_sesion_no_cierra_las_demas(cliente_http):
    """Cada dispositivo tiene su ``jti``: el logout retira uno, no la cuenta."""
    movil = _login(cliente_http, settings.seed_cliente_correo, settings.seed_cliente_password)
    tablet = _login(cliente_http, settings.seed_cliente_correo, settings.seed_cliente_password)

    cliente_http.post(f"{RUTA}/auth/logout", json={"refresh_token": movil.json()["refresh_token"]})

    sigue = cliente_http.post(
        f"{RUTA}/auth/refresh", json={"refresh_token": tablet.json()["refresh_token"]}
    )
    assert sigue.status_code == 200, sigue.text


def test_cambiar_el_rol_revoca_los_tokens_de_refresco(api_admin, cliente_http, db):
    """RF-004 flujo 4a, conectado: el gancho de INC-1A ya revoca de verdad."""
    sesion = _login(
        cliente_http, settings.seed_operario_correo, settings.seed_operario_password
    ).json()
    operario = db.scalars(
        select(Usuario).where(Usuario.correo == settings.seed_operario_correo)
    ).one()
    roles = api_admin.get(f"{RUTA}/admin/roles").json()["items"]
    otro = [rol for rol in roles if rol["nombre"] == "recepcionista"][0]

    cambio = api_admin.put(f"{RUTA}/admin/usuarios/{operario.id}/rol", json={"rol_id": otro["id"]})

    assert cambio.status_code == 200, cambio.text
    respuesta = cliente_http.post(
        f"{RUTA}/auth/refresh", json={"refresh_token": sesion["refresh_token"]}
    )
    assert respuesta.status_code == 401


# --------------------------------------------------------------------------
# RF-006 - profile
# --------------------------------------------------------------------------
def test_el_telefono_nuevo_se_ve_al_recargar_el_perfil(api_cliente):
    """RF-006 CA-01."""
    respuesta = api_cliente.patch(f"{RUTA}/perfil", json={"telefono": "987654999"})

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["usuario"]["telefono"] == "987654999"
    assert api_cliente.get(f"{RUTA}/perfil").json()["usuario"]["telefono"] == "987654999"


def test_las_preferencias_de_notificacion_se_guardan(api_cliente):
    """RF-006 entradas: preferencias de notificación e idioma (RF-029 las leerá)."""
    respuesta = api_cliente.patch(
        f"{RUTA}/perfil",
        json={"notificar_push": False, "idioma": "en", "foto_perfil_key": "perfiles/ana.jpg"},
    )

    assert respuesta.status_code == 200, respuesta.text
    perfil = api_cliente.get(f"{RUTA}/perfil").json()["usuario"]
    assert perfil["notificar_push"] is False
    assert perfil["notificar_correo"] is True
    assert perfil["idioma"] == "en"
    assert perfil["foto_perfil_key"] == "perfiles/ana.jpg"


def test_el_correo_anterior_sigue_vigente_hasta_verificar(api_cliente, cliente_http):
    """RF-006 CA-02: «cuando no se verifica, el correo anterior sigue vigente»."""
    PROVEEDOR_CORREO_PREDETERMINADO.enviados.clear()

    respuesta = api_cliente.patch(f"{RUTA}/perfil", json={"correo": "ana.nueva@example.com"})

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["usuario"]["correo"] == settings.seed_cliente_correo
    assert cuerpo["correo_pendiente"] == "ana.nueva@example.com"
    # El correo anterior es el que sigue abriendo sesión.
    vigente = _login(cliente_http, settings.seed_cliente_correo, settings.seed_cliente_password)
    nuevo = _login(cliente_http, "ana.nueva@example.com", settings.seed_cliente_password)
    assert vigente.status_code == 200
    assert nuevo.status_code == 401


def test_verificar_el_correo_nuevo_lo_aplica(api_cliente, cliente_http):
    """RF-006 flujo 3a: el cambio se aplica al usar el enlace, no antes."""
    PROVEEDOR_CORREO_PREDETERMINADO.enviados.clear()
    api_cliente.patch(f"{RUTA}/perfil", json={"correo": "ana.nueva@example.com"})
    token = _token_del_correo("ana.nueva@example.com")

    respuesta = cliente_http.post(f"{RUTA}/auth/verificacion", json={"token": token})

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["correo"] == "ana.nueva@example.com"
    assert (
        _login(cliente_http, "ana.nueva@example.com", settings.seed_cliente_password).status_code
        == 200
    )


def test_un_correo_ya_registrado_no_puede_pedirse(api_cliente):
    """El cambio de correo respeta la unicidad de RF-001 CA-02."""
    respuesta = api_cliente.patch(f"{RUTA}/perfil", json={"correo": settings.seed_admin_correo})

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "CORREO_YA_REGISTRADO"


def test_un_error_de_validacion_senala_el_campo_y_conserva_lo_demas(api_cliente):
    """RF-006 flujo 4a: se señala el campo y no se pierde el resto.

    La petición rechazada no escribe NADA, así que los valores que el cliente
    ya tenía siguen ahí y el formulario se reenvía con un solo campo corregido.
    """
    antes = api_cliente.get(f"{RUTA}/perfil").json()["usuario"]

    respuesta = api_cliente.patch(f"{RUTA}/perfil", json={"nombres": "Anita", "telefono": "12"})

    assert respuesta.status_code == 422
    detalles = respuesta.json()["error"]["detalles"]
    assert any(detalle["campo"] == "telefono" for detalle in detalles)
    despues = api_cliente.get(f"{RUTA}/perfil").json()["usuario"]
    assert despues["nombres"] == antes["nombres"]
    assert despues["telefono"] == antes["telefono"]


def test_el_perfil_exige_sesion(cliente_http):
    assert cliente_http.get(f"{RUTA}/perfil").status_code == 401
    assert cliente_http.patch(f"{RUTA}/perfil", json={"nombres": "X"}).status_code == 401
