"""Role and permission administration (RF-004 v1.0).

The MVP assigned roles "by migration or console". v1.0 adds the module: list
the roles with what they grant, list the permission catalogue and change the
role of a user, with the two rules of the requirement (the administrator may
not take their own administration away, and the change drops the affected
user's refresh tokens) and the audit entry with the previous and the new value.
"""

from sqlalchemy import select

from app.models import EventoDominio, Rol, Usuario
from app.services import eventos
from tests.conftest import RUTA, codigo_error


def _rol(db, nombre: str) -> Rol:
    rol = db.scalars(select(Rol).where(Rol.nombre == nombre)).first()
    assert rol is not None, f"El seed debería crear el rol «{nombre}»"
    return rol


def _intento_de_check_in(api) -> int:
    """Status of a counter-only call: 403 without the permission, 404 with it."""
    return api.get(f"{RUTA}/reservas/buscar", params={"codigo": "AQL-X"}).status_code


def _eventos(db, accion: str) -> list[EventoDominio]:
    return list(db.scalars(select(EventoDominio).where(EventoDominio.accion == accion)).all())


# --------------------------------------------------------------------------
# The four roles of v1.0
# --------------------------------------------------------------------------
def test_el_catalogo_de_roles_expone_los_cuatro_roles_y_sus_permisos(api_admin):
    """RF-004 paso 1: el administrador ve qué puede hacer cada rol."""
    respuesta = api_admin.get(f"{RUTA}/admin/roles")

    assert respuesta.status_code == 200, respuesta.text
    roles = {item["nombre"]: item for item in respuesta.json()["items"]}
    assert set(roles) == {"cliente", "recepcionista", "operario", "administrador"}
    assert "personal" not in roles, "v1.0 divide el rol del MVP"

    # El reparto: el mostrador recibe y entrega, la bahía avanza el servicio.
    assert "reserva:check_in" in roles["recepcionista"]["permisos"]
    assert "reserva:asignar" in roles["recepcionista"]["permisos"]
    assert "reserva:avanzar_estado" not in roles["recepcionista"]["permisos"]
    assert "reserva:avanzar_estado" in roles["operario"]["permisos"]
    assert "reserva:check_out" not in roles["operario"]["permisos"]
    # El administrador sigue siendo el superconjunto, por datos.
    assert set(roles["administrador"]["permisos"]) >= set(roles["recepcionista"]["permisos"])


def test_el_catalogo_de_permisos_incluye_los_nuevos_de_v1(api_admin):
    """RF-004 paso 2: los códigos que v1.0 añade existen como dato."""
    respuesta = api_admin.get(f"{RUTA}/admin/permisos")

    assert respuesta.status_code == 200, respuesta.text
    codigos = {item["codigo"] for item in respuesta.json()["items"]}
    assert {
        "reserva:asignar",
        "reserva:revisar",
        "agenda:leer",
        "agenda:administrar",
        "usuario:administrar",
        "rol:administrar",
        "bahia:administrar",
    } <= codigos
    assert all(item["descripcion"] for item in respuesta.json()["items"])


def test_la_administracion_de_roles_exige_su_permiso(api_recepcion, api_operario, cliente_http):
    """RF-004 CA-01 y CA-02."""
    assert cliente_http.get(f"{RUTA}/admin/roles").status_code == 401
    assert api_recepcion.get(f"{RUTA}/admin/roles").status_code == 403
    assert api_operario.get(f"{RUTA}/admin/permisos").status_code == 403


# --------------------------------------------------------------------------
# Assigning a role (RF-004 pasos 3-4)
# --------------------------------------------------------------------------
def test_asignar_un_rol_cambia_los_permisos_efectivos(
    api_admin, api_operario, db, usuario_operario
):
    """El cambio se aplica en la siguiente petición, no en el siguiente login."""
    recepcionista = _rol(db, "recepcionista")
    # Antes: el operario no puede hacer check-in de nada.
    assert _intento_de_check_in(api_operario) == 403

    respuesta = api_admin.put(
        f"{RUTA}/admin/usuarios/{usuario_operario.id}/rol", json={"rol_id": recepcionista.id}
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["rol"] == "recepcionista"
    # Mismo token, permisos nuevos: ``permisos_actuales`` los lee de la base.
    assert _intento_de_check_in(api_operario) == 404


def test_el_cambio_de_rol_queda_en_la_bitacora_con_el_valor_anterior(
    api_admin, db, usuario_operario, usuario_admin
):
    """RF-004 CA-02: autor, fecha y valor anterior."""
    anterior = usuario_operario.rol.nombre
    recepcionista = _rol(db, "recepcionista")

    api_admin.put(
        f"{RUTA}/admin/usuarios/{usuario_operario.id}/rol", json={"rol_id": recepcionista.id}
    )

    registrados = _eventos(db, eventos.USUARIO_ROL_CAMBIADO)
    assert len(registrados) == 1
    evento = registrados[0]
    assert evento.entidad == eventos.ENTIDAD_USUARIO
    assert evento.entidad_id == usuario_operario.id
    assert evento.autor_id == usuario_admin.id
    assert evento.ocurrido_en is not None
    assert evento.datos["rol_anterior"] == anterior
    assert evento.datos["rol_nuevo"] == "recepcionista"


def test_el_cambio_de_rol_invalida_los_tokens_de_refresco(api_admin, db, usuario_operario):
    """RF-004 flujo 4a: el gancho que INC-3 convierte en revocación real."""
    recepcionista = _rol(db, "recepcionista")

    api_admin.put(
        f"{RUTA}/admin/usuarios/{usuario_operario.id}/rol", json={"rol_id": recepcionista.id}
    )

    revocaciones = _eventos(db, eventos.USUARIO_TOKENS_REVOCADOS)
    assert len(revocaciones) == 1
    assert revocaciones[0].entidad_id == usuario_operario.id
    assert revocaciones[0].datos["motivo"] == eventos.USUARIO_ROL_CAMBIADO


def test_el_administrador_no_se_quita_el_rol_a_si_mismo(api_admin, db, usuario_admin):
    """RF-004 flujo 3a."""
    operario = _rol(db, "operario")

    respuesta = api_admin.put(
        f"{RUTA}/admin/usuarios/{usuario_admin.id}/rol", json={"rol_id": operario.id}
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "CAMBIO_DE_ROL_PROPIO"

    db.refresh(usuario_admin)
    assert usuario_admin.rol.nombre == "administrador"
    assert _eventos(db, eventos.USUARIO_ROL_CAMBIADO) == []


def test_un_administrador_puede_ascender_a_otro(api_admin, db, usuario_operario):
    """La regla 3a mira el permiso, no el nombre: ascender a otro sí se puede."""
    administrador = _rol(db, "administrador")

    respuesta = api_admin.put(
        f"{RUTA}/admin/usuarios/{usuario_operario.id}/rol", json={"rol_id": administrador.id}
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["rol"] == "administrador"


def test_asignar_un_rol_inexistente_responde_404(api_admin, usuario_operario):
    respuesta = api_admin.put(
        f"{RUTA}/admin/usuarios/{usuario_operario.id}/rol", json={"rol_id": 9999}
    )

    assert respuesta.status_code == 404
    assert codigo_error(respuesta) == "RECURSO_NO_ENCONTRADO"


def test_asignar_el_rol_de_un_usuario_inexistente_responde_404(api_admin, db):
    respuesta = api_admin.put(
        f"{RUTA}/admin/usuarios/9999/rol", json={"rol_id": _rol(db, "operario").id}
    )

    assert respuesta.status_code == 404


def test_reasignar_el_mismo_rol_no_escribe_bitacora(api_admin, db, usuario_operario):
    """Un cambio que no cambia nada no ensucia la auditoría ni tira sesiones."""
    operario = _rol(db, "operario")

    respuesta = api_admin.put(
        f"{RUTA}/admin/usuarios/{usuario_operario.id}/rol", json={"rol_id": operario.id}
    )

    assert respuesta.status_code == 200, respuesta.text
    assert _eventos(db, eventos.USUARIO_ROL_CAMBIADO) == []
    assert _eventos(db, eventos.USUARIO_TOKENS_REVOCADOS) == []


def test_el_registro_publico_sigue_creando_clientes(cliente_http, db):
    """RF-001: partir ``personal`` no toca el rol con que nace una cuenta."""
    respuesta = cliente_http.post(
        f"{RUTA}/auth/registro",
        json={
            "nombres": "Nuevo",
            "apellidos": "Cliente",
            "correo": "nuevo.cliente@aqualav.pe",
            "telefono": "987111222",
            "tipo_documento": "dni",
            "numero_documento": "72222333",
            "password": "Secreta1234",
            "acepta_politica": True,
        },
    )

    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["rol"] == "cliente"
    creado = db.scalars(select(Usuario).where(Usuario.correo == "nuevo.cliente@aqualav.pe")).first()
    assert creado is not None
