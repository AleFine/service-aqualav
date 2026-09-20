"""Access control by permission (RF-004).

CA-03 is verified "by code inspection": the last test walks the whole ``app``
package with the ``ast`` module and fails if any comparison mentions a role
name. ``app/seed.py`` is the single allowed exception (contract section 3).
"""

import ast
import pathlib

import pytest

from app.deps import requiere_permiso
from tests.conftest import RUTA, codigo_error

RAIZ = pathlib.Path(__file__).resolve().parent.parent / "app"
ARCHIVO_DEL_SEED = RAIZ / "seed.py"
NOMBRES_DE_ROL = {"cliente", "personal", "administrador"}

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


def test_el_personal_no_puede_crear_reservas(api_personal, servicio_medio):
    """El personal no tiene ``reserva:crear``; la autorización no mira el rol."""
    respuesta = api_personal.post(
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


def _constantes_de_rol_en_comparaciones(arbol: ast.AST) -> list[str]:
    """Role names used inside any comparison of a module."""
    encontradas: list[str] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Compare):
            continue
        for operando in [nodo.left, *nodo.comparators]:
            for hijo in ast.walk(operando):
                if (
                    isinstance(hijo, ast.Constant)
                    and isinstance(hijo.value, str)
                    and hijo.value in NOMBRES_DE_ROL
                ):
                    encontradas.append(hijo.value)
    return encontradas


def test_ningun_modulo_compara_el_nombre_de_un_rol():
    """RF-004 CA-03: la autorización es por permiso, nunca por nombre de rol."""
    revisados = 0
    infracciones: list[str] = []

    for archivo in sorted(RAIZ.rglob("*.py")):
        if archivo == ARCHIVO_DEL_SEED:
            continue
        revisados += 1
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nombre in _constantes_de_rol_en_comparaciones(arbol):
            infracciones.append(f"{archivo.relative_to(RAIZ)}: compara con «{nombre}»")

    assert revisados > 10, "el recorrido debería cubrir todo el paquete"
    assert infracciones == []
