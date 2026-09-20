"""Internal user administration (RF-035).

The administrator registers the people who work at the shop, gives them a role
and a usual bay, and can switch an account off. Two acceptance criteria carry
the weight: a deactivated worker gets 403 at login (CA-01) and an account
holding services in progress refuses to be deactivated (CA-02).
"""

import re

from sqlalchemy import select

from app.models import Bahia, EventoDominio, Usuario
from app.services.proveedores.correo import PROVEEDOR_CORREO_PREDETERMINADO
from tests.conftest import (
    RUTA,
    asignar,
    codigo_error,
    crear_reserva,
    instante,
    proximo_lunes,
)

NUEVO = {
    "nombres": "Rosa",
    "apellidos": "Ccahuana",
    "correo": "rosa@aqualav.pe",
    "telefono": "987111222",
}


def _rol_id(api_admin, nombre: str) -> int:
    roles = api_admin.get(f"{RUTA}/admin/roles").json()["items"]
    return [rol for rol in roles if rol["nombre"] == nombre][0]["id"]


def _crear(api_admin, **extra):
    cuerpo = dict(NUEVO)
    cuerpo.update(extra)
    return api_admin.post(f"{RUTA}/admin/usuarios", json=cuerpo)


def _password_enviada(correo: str) -> str:
    """Read the temporary password out of the simulated mailbox."""
    mensajes = [msg for msg in PROVEEDOR_CORREO_PREDETERMINADO.enviados if msg.destino == correo]
    assert mensajes, "el proveedor de correo simulado debería tener el mensaje"
    encontrado = re.search(r"Tu contraseña temporal es: (\S+)", mensajes[-1].cuerpo)
    assert encontrado, mensajes[-1].cuerpo
    return encontrado.group(1)


# --------------------------------------------------------------------------
# Creating an internal account
# --------------------------------------------------------------------------
def test_el_alta_envia_la_contrasena_temporal_por_correo(api_admin, cliente_http, db):
    """RF-035 salida: «contraseña temporal enviada al correo del trabajador».

    El proveedor de correo es el simulado del §4 del plan: no abre un socket,
    persiste el mensaje y lo deja disponible para inspección.
    """
    PROVEEDOR_CORREO_PREDETERMINADO.enviados.clear()
    rol_id = _rol_id(api_admin, "operario")

    respuesta = _crear(api_admin, rol_id=rol_id)

    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["correo"] == NUEVO["correo"]
    assert cuerpo["rol"] == "operario"
    assert cuerpo["estado_cuenta"] == "activa"
    assert "password" not in str(cuerpo), "la respuesta nunca lleva la contraseña"

    # Y la contraseña enviada sirve de verdad para entrar.
    password = _password_enviada(NUEVO["correo"])
    login = cliente_http.post(
        f"{RUTA}/auth/login", json={"correo": NUEVO["correo"], "password": password}
    )
    assert login.status_code == 200, login.text


def test_la_bitacora_no_guarda_la_contrasena(api_admin, db):
    """RNF-014: sin contraseñas ni tokens en el registro de eventos."""
    _crear(api_admin, rol_id=_rol_id(api_admin, "operario"))

    eventos = db.scalars(select(EventoDominio).where(EventoDominio.accion.like("usuario.%"))).all()
    acciones = {evento.accion for evento in eventos}
    assert "usuario.creado" in acciones
    assert "usuario.password_temporal_enviada" in acciones
    for evento in eventos:
        assert "password" not in str(evento.datos).lower()
        assert "contraseña" not in str(evento.datos).lower()


def test_el_alta_acepta_bahia_habitual(api_admin, db):
    """RF-035: «rol y bahía habitual»."""
    bahia = db.scalars(select(Bahia).order_by(Bahia.id)).first()

    respuesta = _crear(api_admin, rol_id=_rol_id(api_admin, "operario"), bahia_habitual_id=bahia.id)

    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["bahia_habitual"]["id"] == bahia.id


def test_el_correo_repetido_se_rechaza(api_admin):
    """RF-035 flujo 2a."""
    rol_id = _rol_id(api_admin, "operario")
    assert _crear(api_admin, rol_id=rol_id).status_code == 201

    respuesta = _crear(api_admin, rol_id=rol_id)

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "CORREO_YA_REGISTRADO"


def test_un_rol_inexistente_responde_404(api_admin):
    respuesta = _crear(api_admin, rol_id=9999)

    assert respuesta.status_code == 404
    assert codigo_error(respuesta) == "RECURSO_NO_ENCONTRADO"


def test_una_bahia_habitual_inexistente_responde_404(api_admin):
    respuesta = _crear(api_admin, rol_id=_rol_id(api_admin, "operario"), bahia_habitual_id=9999)

    assert respuesta.status_code == 404


# --------------------------------------------------------------------------
# RF-035 CA-01 - a deactivated account cannot sign in
# --------------------------------------------------------------------------
def test_un_usuario_desactivado_recibe_403_al_iniciar_sesion(api_admin, cliente_http, db):
    """RF-035 CA-01."""
    PROVEEDOR_CORREO_PREDETERMINADO.enviados.clear()
    creado = _crear(api_admin, rol_id=_rol_id(api_admin, "operario")).json()
    password = _password_enviada(NUEVO["correo"])
    credenciales = {"correo": NUEVO["correo"], "password": password}
    assert cliente_http.post(f"{RUTA}/auth/login", json=credenciales).status_code == 200

    baja = api_admin.patch(f"{RUTA}/admin/usuarios/{creado['id']}", json={"activa": False})

    assert baja.status_code == 200, baja.text
    assert baja.json()["estado_cuenta"] == "suspendida"

    respuesta = cliente_http.post(f"{RUTA}/auth/login", json=credenciales)

    assert respuesta.status_code == 403
    assert codigo_error(respuesta) == "CUENTA_DESACTIVADA"


def test_reactivar_la_cuenta_devuelve_el_acceso(api_admin, cliente_http):
    PROVEEDOR_CORREO_PREDETERMINADO.enviados.clear()
    creado = _crear(api_admin, rol_id=_rol_id(api_admin, "operario")).json()
    credenciales = {"correo": NUEVO["correo"], "password": _password_enviada(NUEVO["correo"])}
    api_admin.patch(f"{RUTA}/admin/usuarios/{creado['id']}", json={"activa": False})

    alta = api_admin.patch(f"{RUTA}/admin/usuarios/{creado['id']}", json={"activa": True})

    assert alta.status_code == 200, alta.text
    assert cliente_http.post(f"{RUTA}/auth/login", json=credenciales).status_code == 200


# --------------------------------------------------------------------------
# RF-035 CA-02 / flow 4a - services in progress block the deactivation
# --------------------------------------------------------------------------
def test_no_se_desactiva_un_operario_con_servicios_en_curso(
    api_cliente, api_recepcion, api_admin, db, servicio_medio, vehiculo_id, usuario_operario
):
    """RF-035 CA-02 / flujo 4a."""
    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(dias_minimos=2), 10, 0)
    ).json()
    api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-in", json={"confirmar_retraso": False}
    )
    asignada = asignar(api_recepcion, reserva["id"], operario_id=usuario_operario.id)
    assert asignada.status_code == 200, asignada.text

    respuesta = api_admin.patch(
        f"{RUTA}/admin/usuarios/{usuario_operario.id}", json={"activa": False}
    )

    assert respuesta.status_code == 409
    assert codigo_error(respuesta) == "USUARIO_CON_SERVICIOS_EN_CURSO"
    assert str(reserva["id"]) in str(respuesta.json()["error"]["detalles"])
    db.refresh(usuario_operario)
    assert usuario_operario.estado_cuenta == "activa"


def test_tras_entregar_el_operario_ya_se_puede_desactivar(
    api_cliente,
    api_recepcion,
    api_operario,
    api_admin,
    db,
    servicio_medio,
    vehiculo_id,
    usuario_operario,
):
    """La salida del flujo 4a: cerrar o reasignar y volver a intentar."""
    from tests.conftest import llevar_hasta_finalizado

    reserva = crear_reserva(
        api_cliente, servicio_medio.id, vehiculo_id, instante(proximo_lunes(dias_minimos=2), 10, 0)
    ).json()
    llevar_hasta_finalizado(api_recepcion, api_operario, db, reserva["id"])
    api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/pagos",
        json={"medio": "efectivo", "monto_centimos": 2500},
        headers={"Idempotency-Key": "baja-1"},
    )
    api_recepcion.post(
        f"{RUTA}/reservas/{reserva['id']}/check-out", json={"conformidad_cliente": True}
    )

    respuesta = api_admin.patch(
        f"{RUTA}/admin/usuarios/{usuario_operario.id}", json={"activa": False}
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado_cuenta"] == "suspendida"


# --------------------------------------------------------------------------
# Editing
# --------------------------------------------------------------------------
def test_editar_cambia_los_datos_y_deja_bitacora(api_admin, db):
    creado = _crear(api_admin, rol_id=_rol_id(api_admin, "operario")).json()

    respuesta = api_admin.patch(
        f"{RUTA}/admin/usuarios/{creado['id']}",
        json={"nombres": "Rosa María", "telefono": "987333444"},
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["nombres"] == "Rosa María"
    assert respuesta.json()["telefono"] == "987333444"
    evento = db.scalars(
        select(EventoDominio).where(EventoDominio.accion == "usuario.actualizado")
    ).first()
    assert evento.datos["nombres"]["anterior"] == "Rosa"


def test_editar_el_rol_reutiliza_las_reglas_de_rf_004(api_admin, db):
    """Cambiar el rol desde RF-035 invalida los tokens igual que RF-004 4a."""
    creado = _crear(api_admin, rol_id=_rol_id(api_admin, "operario")).json()

    respuesta = api_admin.patch(
        f"{RUTA}/admin/usuarios/{creado['id']}",
        json={"rol_id": _rol_id(api_admin, "recepcionista")},
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["rol"] == "recepcionista"
    acciones = {
        evento.accion
        for evento in db.scalars(
            select(EventoDominio).where(EventoDominio.entidad_id == creado["id"])
        ).all()
    }
    assert "usuario.rol_cambiado" in acciones
    assert "usuario.tokens_revocados" in acciones


def test_el_administrador_no_se_degrada_a_si_mismo_desde_esta_pantalla(api_admin, usuario_admin):
    """RF-004 flujo 3a sigue cerrado también por la puerta de RF-035."""
    respuesta = api_admin.patch(
        f"{RUTA}/admin/usuarios/{usuario_admin.id}",
        json={"rol_id": _rol_id(api_admin, "cliente")},
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "CAMBIO_DE_ROL_PROPIO"


def test_listar_usuarios_pagina_y_filtra(api_admin):
    rol_id = _rol_id(api_admin, "operario")
    _crear(api_admin, rol_id=rol_id)

    respuesta = api_admin.get(f"{RUTA}/admin/usuarios", params={"rol_id": rol_id})

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 2, "el operario del seed y el recién creado"
    assert all(item["rol"] == "operario" for item in cuerpo["items"])


def test_la_administracion_de_usuarios_exige_su_permiso(
    api_cliente, api_recepcion, api_operario, cliente_http
):
    assert cliente_http.get(f"{RUTA}/admin/usuarios").status_code == 401
    assert api_cliente.get(f"{RUTA}/admin/usuarios").status_code == 403
    assert api_recepcion.get(f"{RUTA}/admin/usuarios").status_code == 403
    assert api_operario.get(f"{RUTA}/admin/usuarios").status_code == 403


def test_editar_un_usuario_inexistente_responde_404(api_admin):
    assert api_admin.patch(f"{RUTA}/admin/usuarios/9999", json={"nombres": "X"}).status_code == 404


def test_el_telefono_se_normaliza_como_en_el_registro_publico(api_admin, db):
    creado = _crear(api_admin, rol_id=_rol_id(api_admin, "operario"), telefono="+51 987 111 222")

    assert creado.status_code == 201, creado.text
    usuario = db.scalars(select(Usuario).where(Usuario.correo == NUEVO["correo"])).first()
    assert usuario.telefono == "987111222"
