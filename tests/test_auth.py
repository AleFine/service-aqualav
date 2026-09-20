"""Registration, login, lockout and refresh (RF-001, RF-002)."""

from sqlalchemy import select

from app.config import settings
from app.models import Usuario
from tests.conftest import RUTA, codigo_error

NUEVO = {
    "nombres": "Ana",
    "apellidos": "Torres",
    "correo": "ana.torres@example.com",
    "telefono": "987654321",
    "password": "Aqua1234",
    "acepta_politica": True,
}


def test_registro_crea_la_cuenta(cliente_http):
    """RF-001 CA-01: a valid form answers 201 and creates the account."""
    respuesta = cliente_http.post(f"{RUTA}/auth/registro", json=NUEVO)

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["correo"] == NUEVO["correo"]
    assert cuerpo["estado_cuenta"] == "activa"
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
