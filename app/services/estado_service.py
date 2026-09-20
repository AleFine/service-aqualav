"""The state catalogue, derived entirely from ``transicion_estado``.

EXTENSION POINT P3. Aggregate screens (the staff board, the history filters,
the timeline) need the WHOLE set of states, not just the moves available to one
reservation. Without this endpoint each of them had to enumerate the five states
of the MVP locally, which is why a reservation in a state added as data dropped
off the staff board entirely.

Nothing here is declared: the set of states, which ones are terminal, which ones
form the main flow and the display order are all read from the table.
"""

from sqlalchemy.orm import Session

from app.models import TransicionEstado
from app.repositories import transicion as transicion_repo
from app.schemas import EstadoCatalogoOut


class _Grafo:
    """The declared state machine as a graph, plus a stable ordering key.

    ``aparicion`` is the position of the row that first mentioned a state. It is
    the tie breaker everywhere, so two runs over the same table always produce
    the same catalogue.
    """

    def __init__(self, transiciones: list[TransicionEstado]) -> None:
        self.salientes: dict[str, list[str]] = {}
        self.entrantes: dict[str, int] = {}
        self.aparicion: dict[str, int] = {}

        for posicion, transicion in enumerate(transiciones):
            for estado in (transicion.estado_origen, transicion.estado_destino):
                self.aparicion.setdefault(estado, posicion)
                self.entrantes.setdefault(estado, 0)
                self.salientes.setdefault(estado, [])
            self.salientes[transicion.estado_origen].append(transicion.estado_destino)
            self.entrantes[transicion.estado_destino] += 1

    @property
    def estados(self) -> list[str]:
        return list(self.aparicion)

    def es_terminal(self, estado: str) -> bool:
        """No declared move leaves this state."""
        return not self.salientes.get(estado)

    def raices(self) -> list[str]:
        """States no declared move leads to, ordered by appearance."""
        return sorted(
            (estado for estado, grado in self.entrantes.items() if grado == 0),
            key=lambda estado: self.aparicion[estado],
        )


def _cadena_principal(grafo: _Grafo) -> list[str]:
    """The longest declared path starting from a state nothing leads to.

    That path IS the main flow: an exception branch (cancelling) is short by
    construction, while the happy path grows every time Annex A inserts a step.
    Deriving it instead of listing it is what lets the mobile timeline show the
    steps still ahead for a state that did not exist when the app was built.
    """
    memoria: dict[str, list[str]] = {}
    en_curso: set[str] = set()

    def desde(estado: str) -> list[str]:
        if estado in memoria:
            return memoria[estado]
        if estado in en_curso:
            # The table is data and may describe a cycle; stop instead of
            # recursing forever. The result stays defined, just not "longest".
            return []

        en_curso.add(estado)
        mejor: list[str] = []
        for destino in grafo.salientes.get(estado, ()):
            candidata = desde(destino)
            if len(candidata) > len(mejor) or (
                len(candidata) == len(mejor)
                and candidata
                and grafo.aparicion[candidata[0]] < grafo.aparicion[mejor[0]]
            ):
                mejor = candidata
        en_curso.discard(estado)

        memoria[estado] = [estado, *mejor]
        return memoria[estado]

    raices = grafo.raices()
    if not raices:
        # Every state is reachable: the table describes a cycle and there is no
        # main flow to speak of.
        return []

    return max(
        (desde(raiz) for raiz in raices),
        key=lambda cadena: (len(cadena), -grafo.aparicion[cadena[0]]),
    )


def _orden_de_presentacion(grafo: _Grafo, principal: list[str]) -> list[str]:
    """Topological order of the states, main flow first.

    Kahn's algorithm: a state is emitted once every move that leads to it has
    been emitted, so a state always follows the ones it comes from. Among the
    states ready at the same time the main flow wins, and the rest fall back to
    the appearance tie breaker.
    """
    posicion_principal = {estado: indice for indice, estado in enumerate(principal)}

    def clave(estado: str) -> tuple[int, int]:
        if estado in posicion_principal:
            return (0, posicion_principal[estado])
        return (1, grafo.aparicion[estado])

    grados = dict(grafo.entrantes)
    disponibles = grafo.raices()
    orden: list[str] = []

    while disponibles:
        disponibles.sort(key=clave)
        estado = disponibles.pop(0)
        orden.append(estado)
        for destino in grafo.salientes.get(estado, ()):
            grados[destino] -= 1
            if grados[destino] == 0:
                disponibles.append(destino)

    # A cycle leaves states with a residual in-degree. They still belong to the
    # catalogue, so they are appended rather than dropped.
    restantes = sorted(set(grafo.estados) - set(orden), key=clave)
    return [*orden, *restantes]


def listar(db: Session) -> list[EstadoCatalogoOut]:
    """Every state the table declares, ready for the aggregate screens."""
    grafo = _Grafo(transicion_repo.listar_todas(db))
    principal = _cadena_principal(grafo)
    orden = _orden_de_presentacion(grafo, principal)
    en_el_flujo = set(principal)

    return [
        EstadoCatalogoOut(
            codigo=estado,
            terminal=grafo.es_terminal(estado),
            principal=estado in en_el_flujo,
            orden=indice,
        )
        for indice, estado in enumerate(orden)
    ]
