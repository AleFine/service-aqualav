"""Static guards shared by the three "by code inspection" tests.

Three invariants of this project cannot be proved by calling an endpoint,
because what they forbid is a line of code that nobody has written yet:

* **P5 / RF-004 CA-03** - authorization is decided on a permission code, never
  on a role name (``tests/test_rbac.py``);
* **P3** - the state machine lives in ``transicion_estado``, so no module
  decides anything by naming a state (``tests/test_operacion.py``);
* **RNF-014 / RF-036 CA-02** - the audit trail is insert only, so no module
  updates or deletes one of its rows (``tests/test_auditoria.py``).

The first two versions of these guards only looked inside ``ast.Compare``,
which made them exactly as strong as the temptation to write ``if rol ==
"administrador"`` and no stronger. Five ways of taking the same decision
walked straight past them: a module constant, an ``in`` against a module-level
tuple, a dictionary lookup, a ``match``/``case``, and ``startswith``. This
module closes those five.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not flag every literal that happens to spell a role or a state.
``"cliente"`` and ``"operario"`` are also ordinary domain vocabulary: they are
the key of a notification payload and the heading of a report column, and
``reporte_service`` is right to use them. A guard that cried wolf on those
would be switched off within a week, and a guard that is off proves nothing.

So the rule is about the SHAPE of the use, not about the word: a name is
flagged when it is compared, matched, looked up in a module-level table, used
as a prefix/suffix test, or frozen into a module-level constant - the five
shapes a decision actually takes. A name used as a key of a payload built on
the spot is not a decision and is left alone.
"""

import ast
import pathlib

#: ``str`` methods whose argument is a test on the text, not data. Catching
#: these is what stops ``rol.startswith("administrador")`` from being the way
#: around ``==``.
METODOS_DE_TEXTO = frozenset(
    {
        "startswith",
        "endswith",
        "removeprefix",
        "removesuffix",
        "find",
        "rfind",
        "index",
        "count",
        "casefold",
    }
)


def _nombres_de_modulo(arbol: ast.Module) -> set[str]:
    """Names bound at module level: the tables a decision can be read from.

    Used to tell ``TARIFA_POR_ROL["cliente"]`` - a module-level table indexed
    by a role, which IS a decision - from ``fila["operario"]``, a key of a row
    the function just built, which is not.
    """
    nombres: set[str] = set()
    for sentencia in arbol.body:
        if isinstance(sentencia, ast.Assign):
            nombres.update(
                objetivo.id for objetivo in sentencia.targets if isinstance(objetivo, ast.Name)
            )
        elif isinstance(sentencia, ast.AnnAssign) and isinstance(sentencia.target, ast.Name):
            nombres.add(sentencia.target.id)
    return nombres


def _literales(nodo: ast.AST, nombres: frozenset[str] | set[str]):
    """Every string constant under ``nodo`` that IS one of ``nombres``."""
    for hijo in ast.walk(nodo):
        if isinstance(hijo, ast.Constant) and isinstance(hijo.value, str):
            if hijo.value in nombres:
                yield hijo.value


def _literales_directos(nodo: ast.AST, nombres: frozenset[str] | set[str]):
    """The same, but only ONE level down.

    A module constant is flagged for the names it spells itself - a bare
    string, an element of a literal tuple/list/set, a key or a value of a
    literal dict - and not for the ones buried inside a call it makes.
    ``COLUMNAS_SERVICIOS = (Columna("cliente", "Cliente"), ...)`` describes a
    report, not a role.
    """
    candidatos: list[ast.AST] = []
    if isinstance(nodo, ast.Constant):
        candidatos = [nodo]
    elif isinstance(nodo, ast.Tuple | ast.List | ast.Set):
        candidatos = list(nodo.elts)
    elif isinstance(nodo, ast.Dict):
        candidatos = [clave for clave in nodo.keys if clave is not None] + list(nodo.values)

    for candidato in candidatos:
        if isinstance(candidato, ast.Constant) and isinstance(candidato.value, str):
            if candidato.value in nombres:
                yield candidato.value


def decisiones_por_nombre(
    arbol: ast.Module, nombres: frozenset[str] | set[str]
) -> list[tuple[int, str]]:
    """``(línea, explicación)`` for every decision taken on one of ``nombres``.

    The five shapes, in the order a reader meets them:

    1. ``rol == "administrador"`` and ``rol in ("cliente", ...)`` - a comparison;
    2. ``case "operario":`` - a structural match;
    3. ``TABLA["cliente"]`` - indexing a module-level table with the name;
    4. ``rol.startswith("admin...")`` - a test on the text of the name;
    5. ``ROL_JEFE = "administrador"`` - freezing the name into a module
       constant, which is also how shape 1 hides: ``rol in ROLES_INTERNOS``
       compares against a tuple whose contents live three lines up.
    """
    del_modulo = _nombres_de_modulo(arbol)
    hallazgos: list[tuple[int, str]] = []

    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Compare):
            for operando in [nodo.left, *nodo.comparators]:
                hallazgos += [
                    (nodo.lineno, f"compara con «{nombre}»")
                    for nombre in _literales(operando, nombres)
                ]

        elif isinstance(nodo, ast.match_case):
            hallazgos += [
                (nodo.pattern.lineno, f"hace «case {nombre}»")
                for nombre in _literales(nodo.pattern, nombres)
            ]

        elif isinstance(nodo, ast.Subscript):
            base = nodo.value
            es_tabla = (isinstance(base, ast.Name) and base.id in del_modulo) or isinstance(
                base, ast.Attribute
            )
            if es_tabla:
                hallazgos += [
                    (nodo.lineno, f"indexa una tabla de módulo con «{nombre}»")
                    for nombre in _literales(nodo.slice, nombres)
                ]

        elif isinstance(nodo, ast.Call) and getattr(nodo.func, "attr", None) in METODOS_DE_TEXTO:
            metodo = nodo.func.attr
            argumentos = [*nodo.args, *(clave.value for clave in nodo.keywords)]
            for argumento in argumentos:
                hallazgos += [
                    (nodo.lineno, f"hace {metodo}(«{nombre}»)")
                    for nombre in _literales(argumento, nombres)
                ]

    for sentencia in arbol.body:
        if isinstance(sentencia, ast.Assign | ast.AnnAssign) and sentencia.value is not None:
            hallazgos += [
                (sentencia.lineno, f"congela «{nombre}» en una constante de módulo")
                for nombre in _literales_directos(sentencia.value, nombres)
            ]

    return hallazgos


def modulos(raiz: pathlib.Path, excluidos: set[pathlib.Path] | None = None):
    """``(ruta, árbol)`` for every module under ``raiz``, sorted and stable."""
    fuera = excluidos or set()
    for archivo in sorted(raiz.rglob("*.py")):
        if archivo in fuera or "__pycache__" in archivo.parts:
            continue
        yield archivo, ast.parse(archivo.read_text(encoding="utf-8"))
