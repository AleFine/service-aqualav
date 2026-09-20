"""Mail port and its simulated implementation (plan section 4).

RF-035 has to "send the temporary password to the worker's e-mail". There is no
SMTP server in a student project and there will not be one, so the port exists
and the default implementation writes the message to the application log and
keeps the last ones in memory, which is enough for the counter to read them
during a demo and for the tests to assert on them.

``CorreoSimulado`` can be told to fail deterministically (``fallar=True``),
which is what RF-001 flow 5a and the RF-029 retries will exercise in INC-3 and
INC-5 without ever touching the network.
"""

import logging
from collections import deque
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.config import settings

logger = logging.getLogger("aqualav.correo")

#: How many delivered messages the simulation keeps for inspection.
HISTORIAL_MAXIMO = 50


@dataclass(frozen=True)
class Mensaje:
    """One delivered e-mail, as the simulation remembers it."""

    destino: str
    asunto: str
    cuerpo: str


class EnvioDeCorreoFallido(Exception):
    """Raised by a simulated provider told to fail. Callers decide what it means."""


@runtime_checkable
class ProveedorCorreo(Protocol):
    """Anything able to deliver an e-mail."""

    def enviar(self, destino: str, asunto: str, cuerpo: str) -> None:
        """Deliver one message, or raise :class:`EnvioDeCorreoFallido`."""
        ...


class CorreoSimulado:
    """Deterministic, offline mail provider. Never opens a socket."""

    def __init__(self, registro: logging.Logger | None = None, fallar: bool = False) -> None:
        self._registro = registro or logger
        self._fallar = fallar
        self.enviados: deque[Mensaje] = deque(maxlen=HISTORIAL_MAXIMO)

    def enviar(self, destino: str, asunto: str, cuerpo: str) -> None:
        if self._fallar:
            raise EnvioDeCorreoFallido(destino)
        self.enviados.append(Mensaje(destino=destino, asunto=asunto, cuerpo=cuerpo))
        self._registro.info("correo destino=%s asunto=%s", destino, asunto)

    @property
    def ultimo(self) -> Mensaje | None:
        return self.enviados[-1] if self.enviados else None


#: Registry of implementations, keyed by the value of ``settings.correo_proveedor``.
PROVEEDORES: dict[str, type] = {"simulado": CorreoSimulado}


def proveedor_correo() -> ProveedorCorreo:
    """Build the configured provider. Unknown names fall back to the simulation."""
    return PROVEEDORES.get(settings.correo_proveedor, CorreoSimulado)()


#: Default instance injected by the services, replaced by a spy in the tests.
PROVEEDOR_CORREO_PREDETERMINADO: ProveedorCorreo = proveedor_correo()
