"""Access control by permission (RF-004).

CA-03 is verified "by code inspection": the last test walks the whole ``app``
package with the ``ast`` module and fails if any comparison mentions a role
name. ``app/seed.py`` is the single allowed exception (contract section 3).
"""

import ast
import pathlib

import pytest

from app.deps import requiere_permiso
from app.seed import ROLES
from tests.conftest import RUTA, codigo_error
from tests.guardas_ast import decisiones_por_nombre, modulos

RAIZ = pathlib.Path(__file__).resolve().parent.parent / "app"
ARCHIVO_DEL_SEED = RAIZ / "seed.py"

#: Read from the seed instead of written down here. A shop that adds a fifth
#: role gets it guarded on the same commit; a list frozen in the test would
#: have gone on passing while the new name was compared freely, which is the
#: one failure mode a guard must not have.
NOMBRES_DE_ROL = frozenset(ROLES)

RECURSOS_PROTEGIDOS = [
    ("get", f"{RUTA}/vehiculos"),
    ("get", f"{RUTA}/servicios"),
    ("get", f"{RUTA}/admin/servicios"),
    ("get", f"{RUTA}/reservas"),
    ("get", f"{RUTA}/auth/yo"),
]


@pytest.mark.parametrize(("metodo", "ruta"), RECURSOS_PROTEGIDOS)
def test_sin_token_todo_recurso_protegido_responde_401(cliente_http, metodo, ruta):
    """RF-004 CA-02."""
    respuesta = getattr(cliente_http, metodo)(ruta)

    assert respuesta.status_code == 401
    assert codigo_error(respuesta) == "NO_AUTENTICADO"


def test_un_token_invalido_responde_401(cliente_http):
    """RF-004 flujo 3b."""
    respuesta = cliente_http.get(
        f"{RUTA}/vehiculos", headers={"Authorization": "Bearer no-es-un-token"}
    )

    assert respuesta.status_code == 401


def test_un_cliente_en_un_recurso_de_administracion_responde_403(api_cliente):
    """RF-004 CA-01."""
    respuesta = api_cliente.post(
        f"{RUTA}/admin/servicios",
        json={
            "nombre": "X",
            "descripcion": "Y",
            "categoria": "general",
            "duracion_min": 30,
            "monto_centimos": 1000,
        },
    )

    assert respuesta.status_code == 403
    assert codigo_error(respuesta) == "PERMISO_DENEGADO"


def test_el_recepcionista_no_puede_crear_reservas(api_recepcion, servicio_medio):
    """El mostrador no tiene ``reserva:crear``; la autorización no mira el rol."""
    respuesta = api_recepcion.post(
        f"{RUTA}/reservas",
        json={
            "servicio_id": servicio_medio.id,
            "vehiculo_id": 1,
            "inicio": "2030-01-07T10:00:00-05:00",
        },
    )

    assert respuesta.status_code == 403


def test_el_administrador_tiene_todos_los_permisos(api_admin):
    """El rol ``administrador`` es un superconjunto, por datos, no por código."""
    assert api_admin.get(f"{RUTA}/admin/servicios").status_code == 200
    assert api_admin.get(f"{RUTA}/servicios").status_code == 200
    assert api_admin.get(f"{RUTA}/reservas").status_code == 200


def test_declarar_un_permiso_inexistente_falla_de_inmediato():
    """Una errata en un código de permiso no puede pasar desapercibida."""
    with pytest.raises(ValueError):
        requiere_permiso("reserva:inventado")


def test_ningun_modulo_decide_por_el_nombre_de_un_rol():
    """RF-004 CA-03: la autorización es por permiso, nunca por nombre de rol.

    Se recorre ``app/`` entero buscando las cinco formas en que una decisión
    por nombre de rol puede escribirse (ver ``tests/guardas_ast.py``):
    comparación, ``match``/``case``, índice en una tabla de módulo, prueba
    sobre el texto y constante de módulo. ``app/seed.py`` es la única
    excepción, porque es el sitio que ENSEÑA al sistema qué permisos tiene
    cada rol y no puede consultarlo en ninguna parte.

    ``migrations/`` queda fuera a propósito y no por descuido: una migración
    de datos nombra roles legítimamente —``0003`` reparte los usuarios de
    ``personal`` entre ``recepcionista`` y ``operario``, y de ``0005`` en
    adelante cada una concede sus permisos nuevos por nombre de rol— porque
    ahí el nombre no decide nada, siembra la tabla en la que se decide.
    Incluirlas daría decenas de falsos positivos y la guarda acabaría apagada.
    """
    revisados = 0
    infracciones: list[str] = []

    for archivo, arbol in modulos(RAIZ, excluidos={ARCHIVO_DEL_SEED}):
        revisados += 1
        for linea, explicacion in decisiones_por_nombre(arbol, NOMBRES_DE_ROL):
            infracciones.append(f"{archivo.relative_to(RAIZ)}:{linea}: {explicacion}")

    assert revisados > 10, "el recorrido debería cubrir todo el paquete"
    assert infracciones == []


def test_la_guarda_de_roles_detecta_las_cinco_formas_de_esquivarla():
    """La guarda se prueba a sí misma: un guardián sin test es un adorno.

    La versión anterior solo miraba dentro de ``ast.Compare`` y estas cinco
    formas —comprobadas una por una— pasaban de largo. Si alguien vuelve a
    estrechar la guarda, este test cae antes de que el agujero exista.
    """
    fuente = """
ROL_JEFE = "administrador"
ROLES_INTERNOS = ("recepcionista", "operario")
TARIFA_POR_ROL = {"cliente": 1}

def por_comparacion(rol):
    return rol == "administrador"

def por_tupla_de_modulo(rol):
    return rol in ROLES_INTERNOS

def por_diccionario(rol):
    return TARIFA_POR_ROL["cliente"]

def por_match(rol):
    match rol:
        case "operario":
            return 1
        case _:
            return 0

def por_prefijo(rol):
    return rol.startswith("recepcionista")
"""
    explicaciones = [texto for _, texto in decisiones_por_nombre(ast.parse(fuente), NOMBRES_DE_ROL)]

    for forma in (
        "compara con",
        "case",
        "indexa una tabla de módulo",
        "startswith",
        "congela",
    ):
        assert any(forma in texto for texto in explicaciones), (forma, explicaciones)


def test_la_guarda_de_roles_no_confunde_vocabulario_con_decision():
    """«cliente» y «operario» también son palabras del dominio.

    La columna «Cliente» de un reporte y la clave ``cliente`` del payload de
    una notificación no autorizan nada. Una guarda que las marcase sería una
    guarda que alguien apaga, así que aquí se fija que NO las marca.
    """
    fuente = """
COLUMNAS = (Columna("cliente", "Cliente"), Columna("operario", "Operario"))

def payload(reserva, fila):
    datos = {"cliente": reserva.usuario.nombres, "operario": fila["operario"]}
    return datos
"""
    assert decisiones_por_nombre(ast.parse(fuente), NOMBRES_DE_ROL) == []
