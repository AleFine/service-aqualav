"""Registration, login, lockout and refresh (RF-001, RF-002)."""

import re

from sqlalchemy import select

from app.config import settings
from app.models import EstadoCuenta, EventoDominio, Usuario, VerificacionCorreo
from app.repositories import autenticacion as autenticacion_repo
from app.schemas import RegistroIn
from app.services import auth_service, eventos
from app.services.proveedores.correo import CorreoSimulado
from tests.conftest import RUTA, codigo_error

NUEVO = {
    "nombres": "Ana",
    "apellidos": "Torres",
    "correo": "ana.torres@example.com",
    "telefono": "987654321",
    "tipo_documento": "dni",
    "numero_documento": "70123456",
    "password": "Aqua1234",
    "acepta_politica": True,
}


def test_registro_crea_la_cuenta(cliente_http):
    """RF-001 CA-01: a valid form answers 201 and creates the account.

    The state changed with v1.0: the MVP quoted "the account is ACTIVE at
    once: there is no verification by e-mail", and the v1.0 postcondition
    leaves it "pending verification" instead. Same CA, different postcondition.
    """
    respuesta = cliente_http.post(f"{RUTA}/auth/registro", json=NUEVO)

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["correo"] == NUEVO["correo"]
    assert cuerpo["estado_cuenta"] == EstadoCuenta.PENDIENTE_VERIFICACION.value
    assert cuerpo["numero_documento"] == NUEVO["numero_documento"]
    assert "hash_password" not in cuerpo


def test_registro_con_correo_repetido_responde_409(cliente_http):
    """RF-001 CA-02: a taken e-mail is refused without creating a duplicate."""
    assert cliente_http.post(f"{RUTA}/auth/registro", json=NUEVO).status_code == 201

    respuesta = cliente_http.post(f"{RUTA}/auth/registro", json=NUEVO)

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "CORREO_YA_REGISTRADO"


def test_la_contrasena_nunca_se_guarda_en_claro(cliente_http, db):
    """RF-001 CA-03: the database stores a bcrypt hash, never the password."""
    assert cliente_http.post(f"{RUTA}/auth/registro", json=NUEVO).status_code == 201

    usuario = db.scalars(select(Usuario).where(Usuario.correo == NUEVO["correo"])).one()

    assert usuario.hash_password != NUEVO["password"]
    assert usuario.hash_password.startswith("$2")
    assert NUEVO["password"] not in usuario.hash_password


def test_registro_rechaza_contrasena_debil(cliente_http):
    """RF-001 flow 4b: the policy is re-validated on the server (RNF-009 M1)."""
    debil = dict(NUEVO, password="abc")

    respuesta = cliente_http.post(f"{RUTA}/auth/registro", json=debil)

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "VALIDACION"


def test_login_emite_los_dos_tokens(cliente_http):
    """RF-002 CA-01: a 30 minute access token plus the refresh token."""
    respuesta = cliente_http.post(
        f"{RUTA}/auth/login",
        json={
            "correo": settings.seed_cliente_correo,
            "password": settings.seed_cliente_password,
        },
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["token_type"] == "bearer"
    assert cuerpo["expires_in"] == settings.access_token_expire_minutes * 60
    assert cuerpo["access_token"] and cuerpo["refresh_token"]
    # The client renders its menu from the permission list, never from the role.
    assert "reserva:crear" in cuerpo["permisos"]


def test_login_con_password_incorrecta_responde_401(cliente_http):
    """RF-002 flow 2a: one generic message, no account enumeration."""
    respuesta = cliente_http.post(
        f"{RUTA}/auth/login",
        json={"correo": settings.seed_cliente_correo, "password": "NoEsLaClave1"},
    )

    assert respuesta.status_code == 401
    assert codigo_error(respuesta) == "CREDENCIALES_INVALIDAS"


def test_cinco_intentos_fallidos_bloquean_la_cuenta(cliente_http):
    """RF-002 CA-02: the fifth failure locks the account and answers 429."""
    cliente_http.post(f"{RUTA}/auth/registro", json=NUEVO)
    malas = {"correo": NUEVO["correo"], "password": "ClaveMala1"}

    for _ in range(4):
        assert cliente_http.post(f"{RUTA}/auth/login", json=malas).status_code == 401

    quinto = cliente_http.post(f"{RUTA}/auth/login", json=malas)

    assert quinto.status_code == 429
    assert codigo_error(quinto) == "CUENTA_BLOQUEADA"
    assert quinto.json()["error"]["detalles"], "Debe informar el tiempo de espera"

    # Even the right password has to wait out the lockout.
    correcta = cliente_http.post(
        f"{RUTA}/auth/login",
        json={"correo": NUEVO["correo"], "password": NUEVO["password"]},
    )
    assert correcta.status_code == 429


def test_login_correcto_reinicia_el_contador(cliente_http, db):
    """RF-002: a successful login clears the failed attempts."""
    cliente_http.post(f"{RUTA}/auth/registro", json=NUEVO)
    cliente_http.post(
        f"{RUTA}/auth/login", json={"correo": NUEVO["correo"], "password": "ClaveMala1"}
    )

    exitoso = cliente_http.post(
        f"{RUTA}/auth/login",
        json={"correo": NUEVO["correo"], "password": NUEVO["password"]},
    )

    assert exitoso.status_code == 200
    usuario = db.scalars(select(Usuario).where(Usuario.correo == NUEVO["correo"])).one()
    assert usuario.intentos_fallidos == 0
    assert usuario.bloqueado_hasta is None


def test_refresh_emite_un_par_nuevo(cliente_http):
    """RF-002: the refresh token buys a new access token."""
    sesion = cliente_http.post(
        f"{RUTA}/auth/login",
        json={
            "correo": settings.seed_cliente_correo,
            "password": settings.seed_cliente_password,
        },
    ).json()

    respuesta = cliente_http.post(
        f"{RUTA}/auth/refresh", json={"refresh_token": sesion["refresh_token"]}
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["access_token"]


def test_refresh_rechaza_un_access_token(cliente_http):
    """Contract section 4: the ``type`` claim is checked in both directions."""
    sesion = cliente_http.post(
        f"{RUTA}/auth/login",
        json={
            "correo": settings.seed_cliente_correo,
            "password": settings.seed_cliente_password,
        },
    ).json()

    respuesta = cliente_http.post(
        f"{RUTA}/auth/refresh", json={"refresh_token": sesion["access_token"]}
    )

    assert respuesta.status_code == 401


def test_yo_devuelve_al_usuario_autenticado(api_cliente):
    respuesta = api_cliente.get(f"{RUTA}/auth/yo")

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["correo"] == settings.seed_cliente_correo


def test_yo_sin_token_responde_401(cliente_http):
    """RF-004 CA-02."""
    respuesta = cliente_http.get(f"{RUTA}/auth/yo")

    assert respuesta.status_code == 401
    assert codigo_error(respuesta) == "NO_AUTENTICADO"


def test_health_vive_bajo_el_prefijo_del_contrato(cliente_http):
    """RNF-010 sobre la ruta que exige RNF-005 M4."""
    assert cliente_http.get(f"{RUTA}/health").json() == {"status": "ok"}
    assert (
        cliente_http.get("/health").status_code == 404
    ), "RNF-005 M4: todo recurso del contrato vive bajo /api/v1"


def test_ningun_recurso_queda_fuera_del_prefijo(cliente_http):
    """RNF-005 M4, comprobado sobre el esquema publicado.

    ``/docs``, ``/redoc`` y ``/openapi.json`` no cuentan: RNF-005 M1 los exige
    exactamente en esas rutas y FastAPI no los declara en ``paths``.
    """
    esquema = cliente_http.get("/openapi.json").json()

    fuera = [ruta for ruta in esquema["paths"] if not ruta.startswith(RUTA)]

    assert fuera == []


# --------------------------------------------------------------------------
# RF-001 delta v1.0 - document, verification and flow 5a
# --------------------------------------------------------------------------
def _ultimo_token(correo: str) -> str:
    """The token of the last simulated mail sent to ``correo``."""
    mensajes = [
        m for m in auth_service.PROVEEDOR_CORREO_PREDETERMINADO.enviados if m.destino == correo
    ]
    assert mensajes, f"el proveedor simulado debería tener un mensaje para {correo}"
    encontrado = re.search(r"token=(\S+)", mensajes[-1].cuerpo)
    assert encontrado, mensajes[-1].cuerpo
    return encontrado.group(1)


def test_el_documento_tiene_unicidad_propia(cliente_http):
    """RF-001 v1.0 paso 4: «validar su unicidad además de la del correo»."""
    assert cliente_http.post(f"{RUTA}/auth/registro", json=NUEVO).status_code == 201

    otro = dict(NUEVO, correo="otra.persona@example.com")
    respuesta = cliente_http.post(f"{RUTA}/auth/registro", json=otro)

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "DOCUMENTO_YA_REGISTRADO"


def test_el_dni_exige_ocho_digitos(cliente_http):
    """RF-001 paso 4: el DNI peruano tiene una forma propia."""
    respuesta = cliente_http.post(f"{RUTA}/auth/registro", json=dict(NUEVO, numero_documento="123"))

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "VALIDACION"


def test_el_registro_envia_el_correo_de_verificacion(cliente_http, db):
    """RF-001 paso 5: la cuenta nace pendiente y sale un correo con el enlace."""
    auth_service.PROVEEDOR_CORREO_PREDETERMINADO.enviados.clear()

    assert cliente_http.post(f"{RUTA}/auth/registro", json=NUEVO).status_code == 201

    pendientes = db.scalars(
        select(VerificacionCorreo).where(VerificacionCorreo.correo == NUEVO["correo"])
    ).all()
    assert len(pendientes) == 1
    assert pendientes[0].verificado_en is None
    # El token viaja en el correo; en la base solo vive su huella.
    assert _ultimo_token(NUEVO["correo"]) not in pendientes[0].token_hash


def test_verificar_el_correo_activa_la_cuenta(cliente_http, db):
    """RF-001 postcondición: verificar es lo que pasa la cuenta a «activa»."""
    auth_service.PROVEEDOR_CORREO_PREDETERMINADO.enviados.clear()
    cliente_http.post(f"{RUTA}/auth/registro", json=NUEVO)
    token = _ultimo_token(NUEVO["correo"])

    respuesta = cliente_http.post(f"{RUTA}/auth/verificacion", json={"token": token})

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado_cuenta"] == EstadoCuenta.ACTIVA.value


def test_el_enlace_de_verificacion_no_sirve_dos_veces(cliente_http):
    """El token de verificación también es de un solo uso."""
    auth_service.PROVEEDOR_CORREO_PREDETERMINADO.enviados.clear()
    cliente_http.post(f"{RUTA}/auth/registro", json=NUEVO)
    token = _ultimo_token(NUEVO["correo"])
    assert cliente_http.post(f"{RUTA}/auth/verificacion", json={"token": token}).status_code == 200

    respuesta = cliente_http.post(f"{RUTA}/auth/verificacion", json={"token": token})

    assert respuesta.status_code == 400
    assert codigo_error(respuesta) == "TOKEN_INVALIDO"


def test_si_falla_el_envio_la_cuenta_se_crea_igual(db):
    """RF-001 flujo 5a: «la cuenta se crea en estado pendiente y se reintenta»."""
    datos = RegistroIn.model_validate(NUEVO)

    usuario = auth_service.registrar(db, datos, correo_proveedor=CorreoSimulado(fallar=True))

    assert usuario.id is not None
    assert usuario.estado_cuenta == EstadoCuenta.PENDIENTE_VERIFICACION.value
    acciones = [
        evento.accion
        for evento in db.scalars(
            select(EventoDominio).where(EventoDominio.entidad_id == usuario.id)
        ).all()
    ]
    assert eventos.USUARIO_VERIFICACION_NO_ENVIADA in acciones

    # Y el reintento del flujo 5a sí entrega el enlace.
    auth_service.PROVEEDOR_CORREO_PREDETERMINADO.enviados.clear()
    auth_service.reenviar_verificacion(db, NUEVO["correo"])
    assert _ultimo_token(NUEVO["correo"])


# --------------------------------------------------------------------------
# RF-002 delta v1.0 - flow 2c
# --------------------------------------------------------------------------
def test_una_cuenta_sin_verificar_inicia_sesion_y_se_le_ofrece_reenviar(cliente_http):
    """RF-002 flujo 2c: «cuenta no verificada: se ofrece reenviar el correo».

    El requisito ofrece el reenvío, no niega el acceso: negarlo dejaría fuera
    para siempre a la cuenta del flujo 5a de RF-001, que existe precisamente
    porque el correo NO se pudo entregar.
    """
    cliente_http.post(f"{RUTA}/auth/registro", json=NUEVO)

    sesion = cliente_http.post(
        f"{RUTA}/auth/login", json={"correo": NUEVO["correo"], "password": NUEVO["password"]}
    )

    assert sesion.status_code == 200, sesion.text
    assert sesion.json()["verificacion_pendiente"] is True

    auth_service.PROVEEDOR_CORREO_PREDETERMINADO.enviados.clear()
    reenvio = cliente_http.post(
        f"{RUTA}/auth/verificacion/reenviar", json={"correo": NUEVO["correo"]}
    )
    assert reenvio.status_code == 200, reenvio.text
    assert _ultimo_token(NUEVO["correo"])


def test_una_cuenta_verificada_ya_no_pide_verificacion(cliente_http):
    """El indicador se apaga solo cuando la cuenta se verifica."""
    sesion = cliente_http.post(
        f"{RUTA}/auth/login",
        json={
            "correo": settings.seed_cliente_correo,
            "password": settings.seed_cliente_password,
        },
    )

    assert sesion.json()["verificacion_pendiente"] is False


def test_el_reenvio_responde_igual_para_un_correo_inexistente(cliente_http):
    """No enumeración: el reenvío no distingue una cuenta de una inexistente."""
    existente = cliente_http.post(
        f"{RUTA}/auth/verificacion/reenviar", json={"correo": settings.seed_cliente_correo}
    )
    inexistente = cliente_http.post(
        f"{RUTA}/auth/verificacion/reenviar", json={"correo": "nadie@example.com"}
    )

    assert existente.status_code == inexistente.status_code == 200
    assert existente.json() == inexistente.json()


def test_cada_intento_de_login_queda_en_la_bitacora(cliente_http, db):
    """RNF-014: la trazabilidad de autenticaciones que RF-036 leerá."""
    cliente_http.post(
        f"{RUTA}/auth/login",
        json={"correo": settings.seed_cliente_correo, "password": "NoEsLaClave1"},
    )
    cliente_http.post(
        f"{RUTA}/auth/login",
        json={
            "correo": settings.seed_cliente_correo,
            "password": settings.seed_cliente_password,
        },
    )

    intentos = autenticacion_repo.listar_intentos(db, settings.seed_cliente_correo)

    assert [intento.exitoso for intento in intentos][-2:] == [False, True]
    assert all("password" not in (intento.motivo or "") for intento in intentos)
