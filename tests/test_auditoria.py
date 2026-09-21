"""Bitácora de auditoría (RF-036, RNF-014).

Tres cosas se verifican aquí que no son «un endpoint más»:

* **`CA-01`** — un cambio de precio figura con su valor anterior y el nuevo.
* **`CA-02`** — editar un registro por la API no existe ni se permite. Se
  comprueba por dos caminos: contra el router en caliente (ningún verbo de
  escritura responde) y **por inspección de código** con ``ast``, igual que
  ``test_rbac`` verifica P5 y ``test_operacion`` verifica P3: ningún módulo de
  ``app/`` hace UPDATE ni DELETE sobre las dos tablas de la bitácora.
* **flujo `2a`** — si falla el registro de auditoría, la operación principal se
  revierte. Esta es la parte delicada del incremento y se prueba de la única
  forma que vale: rompiendo el registro a propósito y comprobando que el
  cambio de precio **no quedó**.

La bitácora es una VISTA sobre lo que el sistema ya escribía, no una tabla
nueva: ``evento_dominio`` (P7, desde el MVP) unida a ``intento_login`` (INC-3).
La Parte 5 de las brechas llama al primero «el ancestro de
``bitacora_auditoria``» y dice que se conserva y se amplía; eso es lo que hay.
La consecuencia importante es justamente el flujo `2a`: como la fila se escribe
DENTRO de la transacción del negocio, revertir no es una compensación que
alguien tenga que acordarse de programar.
"""

import ast
import pathlib
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.models import EventoDominio, IntentoLogin, Pago
from app.repositories import evento as evento_repo
from app.services import eventos
from tests.conftest import (
    RUTA,
    TARJETA_APROBADA,
    codigo_error,
    crear_reserva,
    instante,
    pagar_en_linea,
    proximo_lunes,
)

RAIZ_APP = pathlib.Path(__file__).resolve().parent.parent / "app"


def _periodo(dias: int = 30) -> tuple[str, str]:
    hoy = date.today()
    return (hoy - timedelta(days=dias)).isoformat(), (hoy + timedelta(days=dias)).isoformat()


def _bitacora(api_admin, **filtros) -> dict:
    desde, hasta = _periodo()
    consulta = "&".join(f"{clave}={valor}" for clave, valor in filtros.items() if valor is not None)
    sufijo = f"&{consulta}" if consulta else ""
    respuesta = api_admin.get(f"{RUTA}/auditoria?desde={desde}&hasta={hasta}{sufijo}")
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


def _cambiar_precio(api_admin, servicio_id: int, monto: int):
    return api_admin.patch(f"{RUTA}/admin/servicios/{servicio_id}", json={"monto_centimos": monto})


# --------------------------------------------------------------------------
# RF-036 CA-01 - valor anterior y valor nuevo
# --------------------------------------------------------------------------
def test_un_cambio_de_precio_figura_con_el_valor_anterior_y_el_nuevo(api_admin, servicio_medio):
    """RF-036 `CA-01`, literal."""
    anterior = servicio_medio.precio_vigente.monto_centimos
    assert _cambiar_precio(api_admin, servicio_medio.id, anterior + 500).status_code == 200

    pagina = _bitacora(api_admin, accion=eventos.SERVICIO_PRECIO_CAMBIADO)

    assert pagina["total"] == 1
    fila = pagina["items"][0]
    assert fila["valor_anterior"]["monto_centimos"] == anterior
    assert fila["valor_nuevo"]["monto_centimos"] == anterior + 500
    assert fila["autor"], "la bitácora registra quién lo hizo"
    assert fila["ocurrido_en"], "y cuándo"


def test_un_cambio_de_rol_tambien_lleva_los_dos_valores(api_admin, usuario_cliente):
    """RF-036: «cambios de rol» entre las operaciones sensibles."""
    roles = api_admin.get(f"{RUTA}/admin/roles").json()["items"]
    destino = next(rol for rol in roles if rol["id"] != usuario_cliente.rol_id)

    respuesta = api_admin.put(
        f"{RUTA}/admin/usuarios/{usuario_cliente.id}/rol", json={"rol_id": destino["id"]}
    )
    assert respuesta.status_code == 200, respuesta.text

    fila = _bitacora(api_admin, accion=eventos.USUARIO_ROL_CAMBIADO)["items"][0]
    assert fila["valor_anterior"]["rol_id"] != fila["valor_nuevo"]["rol_id"]
    assert fila["valor_nuevo"]["rol_id"] == destino["id"]


# --------------------------------------------------------------------------
# RF-036 - las seis operaciones sensibles que el requisito enumera
# --------------------------------------------------------------------------
def test_las_autenticaciones_aparecen_en_la_bitacora(api_admin, cliente_http):
    """RF-036: «autenticaciones».

    Cierra el hueco que INC-3 dejó anotado: ``intento_login`` se escribía en
    cada intento y nadie la leía. La bitácora la proyecta sin copiarla, que es
    lo que impide tener dos registros del mismo hecho.
    """
    cliente_http.post(
        f"{RUTA}/auth/login", json={"correo": "nadie@aqualav.pe", "password": "Incorrecta1"}
    )

    pagina = _bitacora(api_admin)
    acciones = {fila["accion"] for fila in pagina["items"]}

    assert eventos.USUARIO_AUTENTICACION_EXITOSA in acciones, "el login del admin"
    assert (
        eventos.USUARIO_AUTENTICACION_FALLIDA in acciones
    ), "el intento contra una cuenta que no existe"
    fallido = next(
        fila for fila in pagina["items"] if fila["accion"] == eventos.USUARIO_AUTENTICACION_FALLIDA
    )
    assert fallido["fuente"] == "intento_login"
    assert fallido["detalle"]["correo"] == "nadie@aqualav.pe"
    assert fallido["detalle"]["motivo"] == "CREDENCIALES_INVALIDAS"


def test_una_cancelacion_un_pago_y_un_reembolso_quedan_en_la_bitacora(
    api_admin, api_cliente, db, servicio_medio, vehiculo_id
):
    """RF-036: «cancelaciones, pagos y reembolsos»."""
    reserva = crear_reserva(
        api_cliente,
        servicio_medio.id,
        vehiculo_id,
        instante(proximo_lunes(), 10, 0),
        modalidad_pago="en_linea",
    ).json()
    assert pagar_en_linea(
        api_cliente, reserva["id"], tarjeta=TARJETA_APROBADA, clave="bitacora-1"
    ).status_code in (200, 201)

    pago = db.scalars(select(Pago).where(Pago.reserva_id == reserva["id"])).one()
    assert api_admin.post(
        f"{RUTA}/pagos/{pago.id}/reembolsos",
        json={"tipo": "parcial", "monto_centimos": 100, "motivo": "Ajuste de prueba"},
        headers={"Idempotency-Key": "bitacora-reembolso"},
    ).status_code in (200, 201)

    assert (
        api_cliente.post(
            f"{RUTA}/reservas/{reserva['id']}/cancelacion", json={"motivo": "Cambio de planes"}
        ).status_code
        == 200
    )

    acciones = {fila["accion"] for fila in _bitacora(api_admin, tamanio=50)["items"]}
    assert eventos.PAGO_EN_LINEA_APROBADO in acciones
    assert eventos.REEMBOLSO_PROCESADO in acciones
    assert eventos.RESERVA_CANCELADA in acciones


# --------------------------------------------------------------------------
# RF-036 - consulta paginada con filtros (usuario, tipo de evento, fecha)
# --------------------------------------------------------------------------
def test_la_bitacora_filtra_por_usuario_por_accion_y_por_entidad(
    api_admin, servicio_medio, usuario_admin, usuario_cliente
):
    _cambiar_precio(api_admin, servicio_medio.id, 9900)

    por_autor = _bitacora(api_admin, usuario_id=usuario_admin.id)
    ajeno = _bitacora(api_admin, usuario_id=usuario_cliente.id)
    por_entidad = _bitacora(api_admin, entidad="servicio", entidad_id=servicio_medio.id)

    assert por_autor["total"] >= 1
    assert all(fila["autor_id"] == usuario_admin.id for fila in por_autor["items"])
    assert ajeno["total"] == 0, "el cliente no hizo nada en este test"
    assert por_entidad["total"] >= 1
    assert all(fila["entidad"] == "servicio" for fila in por_entidad["items"])


def test_la_bitacora_ofrece_el_catalogo_de_acciones_del_periodo(api_admin, servicio_medio):
    """El filtro «tipo de evento» se deriva de la propia bitácora.

    Mismo criterio que ``GET /estados`` con ``transicion_estado`` (P3): un
    incremento que empiece a escribir una acción nueva la ve aparecer en el
    filtro sin tocar un catálogo escrito a mano.
    """
    _cambiar_precio(api_admin, servicio_medio.id, 9800)

    pagina = _bitacora(api_admin)

    assert eventos.SERVICIO_PRECIO_CAMBIADO in pagina["acciones"]
    assert eventos.USUARIO_AUTENTICACION_EXITOSA in pagina["acciones"]


def test_la_bitacora_pagina_sin_repetir_ni_perder_registros(api_admin, servicio_medio):
    """RF-036: «consulta paginada»."""
    for monto in (9100, 9200, 9300, 9400):
        assert _cambiar_precio(api_admin, servicio_medio.id, monto).status_code == 200

    primera = _bitacora(api_admin, tamanio=2, pagina=1)
    segunda = _bitacora(api_admin, tamanio=2, pagina=2)

    assert primera["total"] == segunda["total"] >= 5
    assert primera["total_paginas"] >= 3
    assert len(primera["items"]) == 2
    identificadores = {(f["fuente"], f["id"]) for f in primera["items"] + segunda["items"]}
    assert len(identificadores) == 4, "ninguna fila aparece en dos páginas"


def test_una_consulta_de_mas_de_doce_meses_pide_acotar_el_rango(api_admin):
    """RF-036 flujo `3a`: «consulta muy amplia → se pide acotar el rango»."""
    hasta = date.today()
    desde = hasta - timedelta(days=548)

    respuesta = api_admin.get(
        f"{RUTA}/auditoria?desde={desde.isoformat()}&hasta={hasta.isoformat()}"
    )

    assert respuesta.status_code == 422
    assert codigo_error(respuesta) == "RANGO_DEMASIADO_AMPLIO"


def test_la_bitacora_exige_su_propio_permiso(api_recepcion, api_admin):
    """P5, y además: ``reporte:leer`` no abre la bitácora.

    Son dos permisos distintos a propósito. Un rol que deba ver la facturación
    no tiene por qué heredar todos los intentos de sesión del taller.
    """
    desde, hasta = _periodo()

    respuesta = api_recepcion.get(f"{RUTA}/auditoria?desde={desde}&hasta={hasta}")

    assert respuesta.status_code == 403
    assert codigo_error(respuesta) == "PERMISO_DENEGADO"


def test_exportar_la_bitacora_exige_el_permiso_de_auditoria_y_no_el_de_reportes(
    api_admin, api_recepcion
):
    """RF-036: «exportable», por la misma puerta que los reportes de RF-034.

    El permiso se valida DENTRO del servicio, que es el patrón que dejó INC-7:
    si una operación se alcanza por más de una puerta, el permiso no puede
    vivir solo en el router.
    """
    desde, hasta = _periodo()
    cuerpo = {"tipo": "auditoria", "formato": "csv", "desde": desde, "hasta": hasta}

    del_admin = api_admin.post(f"{RUTA}/reportes/exportaciones", json=cuerpo)
    del_mostrador = api_recepcion.post(f"{RUTA}/reportes/exportaciones", json=cuerpo)

    assert del_admin.status_code == 201, del_admin.text
    assert del_admin.json()["tipo"] == "auditoria"
    assert del_mostrador.status_code == 403


# --------------------------------------------------------------------------
# RF-036 CA-02 - solo inserción
# --------------------------------------------------------------------------
@pytest.mark.parametrize("metodo", ["put", "patch", "delete", "post"])
def test_no_existe_forma_de_editar_un_registro_de_la_bitacora_por_la_api(api_admin, metodo):
    """RF-036 `CA-02`: «la operación no existe o se deniega».

    Las dos mitades: sobre la colección el verbo existe pero no está permitido
    (405) y sobre un registro concreto la ruta directamente no existe (404).
    """
    sobre_la_coleccion = getattr(api_admin, metodo)(f"{RUTA}/auditoria")
    sobre_un_registro = getattr(api_admin, metodo)(f"{RUTA}/auditoria/1")

    assert sobre_la_coleccion.status_code == 405
    assert sobre_un_registro.status_code == 404


def test_el_router_de_auditoria_solo_declara_lecturas(cliente_http):
    """No hay verbo de escritura registrado en la aplicación para la bitácora."""
    from app.main import app

    rutas = [ruta for ruta in app.routes if "auditoria" in getattr(ruta, "path", "")]

    assert rutas, "la bitácora está montada"
    for ruta in rutas:
        assert set(ruta.methods) <= {"GET", "HEAD"}, f"{ruta.path} acepta {ruta.methods}"


def _mutaciones_de_la_bitacora(arbol: ast.AST) -> list[str]:
    """Llamadas que modificarían o borrarían una fila de la bitácora.

    Busca ``db.delete(...)``/``session.delete(...)`` con un modelo de la
    bitácora dentro, y cualquier ``update(EventoDominio)`` / ``delete(...)``
    de SQLAlchemy sobre esos modelos.
    """
    modelos = {"EventoDominio", "IntentoLogin"}
    encontradas: list[str] = []

    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        objetivo = nodo.func
        nombre = getattr(objetivo, "attr", None) or getattr(objetivo, "id", None)
        if nombre not in {"delete", "update"}:
            continue
        for argumento in nodo.args:
            for hijo in ast.walk(argumento):
                if isinstance(hijo, ast.Name) and hijo.id in modelos:
                    encontradas.append(f"{nombre}({hijo.id})")
                if isinstance(hijo, ast.Attribute) and hijo.attr in modelos:
                    encontradas.append(f"{nombre}({hijo.attr})")
    return encontradas


def test_ningun_modulo_modifica_ni_borra_la_bitacora():
    """RF-036 `CA-02` y RNF-014 («solo inserción»), por inspección de código.

    El gemelo de ``test_ningun_modulo_compara_el_nombre_de_un_rol`` (P5) y de
    ``test_ningun_modulo_compara_el_nombre_de_un_estado`` (P3): la garantía de
    que la bitácora es inmutable no puede depender de que nadie escriba el
    UPDATE, tiene que depender de que se note si alguien lo escribe.
    """
    revisados = 0
    infracciones: list[str] = []

    for archivo in sorted(RAIZ_APP.rglob("*.py")):
        revisados += 1
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for hallazgo in _mutaciones_de_la_bitacora(arbol):
            infracciones.append(f"{archivo.relative_to(RAIZ_APP)}: {hallazgo}")

    assert revisados > 10, "el recorrido debería cubrir todo el paquete"
    assert infracciones == []


def test_el_repositorio_de_eventos_no_expone_ninguna_escritura_salvo_crear():
    """La única puerta de escritura de la bitácora es ``crear``."""
    publicas = {
        nombre
        for nombre in dir(evento_repo)
        if not nombre.startswith("_") and callable(getattr(evento_repo, nombre))
    }
    escrituras = {
        nombre
        for nombre in publicas
        if any(verbo in nombre for verbo in ("crear", "actualizar", "borrar", "eliminar", "anular"))
    }

    assert escrituras == {"crear"}


# --------------------------------------------------------------------------
# RF-036 2a - si falla la auditoría, la operación principal se revierte
# --------------------------------------------------------------------------
def test_si_falla_el_registro_de_auditoria_el_cambio_de_precio_se_revierte(
    api_admin, db, servicio_medio, monkeypatch
):
    """RF-036 flujo `2a`, la parte delicada del incremento.

    No se prueba que «se llame a un rollback»: se prueba el resultado. Se rompe
    el append de la bitácora, se cambia un precio y se comprueba que el precio
    **siguió siendo el mismo**. Eso solo puede ser cierto si la fila de
    auditoría se escribe dentro de la misma transacción que el negocio, que es
    exactamente por qué la bitácora es ``evento_dominio`` y no una tabla
    aparte escrita después.
    """
    anterior = servicio_medio.precio_vigente.monto_centimos

    def _romper(*args, **kwargs):
        raise SQLAlchemyError("la bitácora no está disponible")

    monkeypatch.setattr(evento_repo, "crear", _romper)

    respuesta = _cambiar_precio(api_admin, servicio_medio.id, anterior + 1500)

    assert respuesta.status_code == 500
    assert codigo_error(respuesta) == "AUDITORIA_NO_REGISTRADA"

    monkeypatch.undo()
    db.expire_all()
    vigente = api_admin.get(f"{RUTA}/admin/servicios").json()["items"]
    precio = next(fila for fila in vigente if fila["id"] == servicio_medio.id)
    assert precio["precio"]["monto_centimos"] == anterior, "la operación principal se revirtió"


def test_si_falla_el_registro_de_auditoria_no_queda_rastro_a_medias(
    api_admin, db, servicio_medio, monkeypatch
):
    """La reversión es total: ni el precio nuevo ni una fila de bitácora."""
    eventos_antes = db.query(EventoDominio).count()

    def _romper(*args, **kwargs):
        raise SQLAlchemyError("la bitácora no está disponible")

    monkeypatch.setattr(evento_repo, "crear", _romper)
    _cambiar_precio(api_admin, servicio_medio.id, 12345)
    monkeypatch.undo()

    db.expire_all()
    assert db.query(EventoDominio).count() == eventos_antes


# --------------------------------------------------------------------------
# RNF-014 - ni contraseñas, ni tokens, ni tarjetas
# --------------------------------------------------------------------------
def test_la_bitacora_no_expone_contrasenas_ni_tokens_ni_tarjetas(
    api_admin, api_cliente, cliente_http, servicio_medio, vehiculo_id
):
    """RNF-014: «sin contraseñas, tokens ni tarjetas».

    El gemelo del test del PAN que dejó INC-4, pero sobre la superficie que
    este incremento abre: todo lo que la bitácora DEVUELVE, no solo lo que la
    pasarela recibe.
    """
    cliente_http.post(
        f"{RUTA}/auth/login",
        json={"correo": settings.seed_cliente_correo, "password": settings.seed_cliente_password},
    )
    reserva = crear_reserva(
        api_cliente,
        servicio_medio.id,
        vehiculo_id,
        instante(proximo_lunes(), 11, 0),
        modalidad_pago="en_linea",
    ).json()
    pagar_en_linea(api_cliente, reserva["id"], tarjeta=TARJETA_APROBADA, clave="rnf014")

    volcado = str(_bitacora(api_admin, tamanio=50))

    assert TARJETA_APROBADA not in volcado, "el PAN nunca entra en la bitácora"
    assert settings.seed_cliente_password not in volcado
    assert settings.seed_admin_password not in volcado
    assert "hash_password" not in volcado


def test_la_bitacora_oculta_cualquier_clave_que_parezca_una_credencial(
    api_admin, db, usuario_admin
):
    """La red de seguridad: un evento mal escrito tampoco filtra nada.

    Ningún llamante de hoy guarda una credencial en un evento, y el test de
    arriba lo comprueba. Este comprueba lo otro: que el día que alguien añada
    un evento sin leer RNF-014, la bitácora no lo publique igualmente.
    """
    eventos.registrar_evento(
        db,
        eventos.ENTIDAD_USUARIO,
        usuario_admin.id,
        eventos.USUARIO_ACTUALIZADO,
        autor_id=usuario_admin.id,
        datos={"password_temporal": "Secreta1234", "token_push": "abc123", "correo": "a@b.pe"},
    )
    db.commit()

    fila = next(
        item
        for item in _bitacora(api_admin, accion=eventos.USUARIO_ACTUALIZADO)["items"]
        if item["detalle"]
    )

    assert fila["detalle"]["password_temporal"] == "[oculto]"
    assert fila["detalle"]["token_push"] == "[oculto]"
    assert fila["detalle"]["correo"] == "a@b.pe", "lo que no es credencial se sigue viendo"


def test_la_tabla_de_intentos_no_guarda_la_contrasena(db, cliente_http):
    """INC-3 ya lo garantizaba; la bitácora lo hereda y aquí se afirma."""
    cliente_http.post(
        f"{RUTA}/auth/login",
        json={"correo": settings.seed_cliente_correo, "password": "UnaClaveMuyMala1"},
    )

    intentos = db.query(IntentoLogin).all()

    assert intentos
    for intento in intentos:
        assert "UnaClaveMuyMala1" not in str(intento.__dict__)
        assert intento.motivo in (None, "CREDENCIALES_INVALIDAS", "CUENTA_BLOQUEADA")
