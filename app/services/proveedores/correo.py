"""Mail port and its simulated implementation (plan section 4).

RF-035 has to "send the temporary password to the worker's e-mail". There is no
SMTP server in a student project and there will not be one, so the port exists
and the default implementation writes the message to the application log and
keeps the last ones in memory, which is enough for the counter to read them
during a demo and for the tests to assert on them.

INC-5 adds the half the MVP was missing: **the message is also persisted**.
RF-029 asks for "registro del resultado del envío para trazabilidad" and CA-02
for the cause of a failure after the retries, and neither fits in a log line.
The PORT did not change - it is still ``enviar(destino, asunto, cuerpo)`` - and
neither did any of its callers: persistence is a property of the instance. A
provider built by :func:`proveedor_correo` with a session and a
``notificacion`` row records every attempt against it; one built without them
behaves exactly as it did in INC-1B.

``CorreoSimulado`` can be told to fail deterministically (``fallar=True``),
which is what RF-001 flow 5a and the RF-029 retries exercise without ever
touching the network.
"""

import logging
from collections import deque
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Notificacion
from app.services.proveedores.registro import anotar

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

    def __init__(
        self,
        registro: logging.Logger | None = None,
        fallar: bool = False,
        *,
        sesion: Session | None = None,
        notificacion: Notificacion | None = None,
    ) -> None:
        self._registro = registro or logger
        self._fallar = fallar
        self._sesion = sesion
        self._notificacion = notificacion
        self.enviados: deque[Mensaje] = deque(maxlen=HISTORIAL_MAXIMO)

    def enviar(self, destino: str, asunto: str, cuerpo: str) -> None:
        if self._fallar:
            causa = f"El proveedor de correo rechazó el envío a {destino}."
            anotar(self._sesion, self._notificacion, error=causa)
            raise EnvioDeCorreoFallido(causa)

        anotar(self._sesion, self._notificacion)
        self.enviados.append(Mensaje(destino=destino, asunto=asunto, cuerpo=cuerpo))
        self._registro.info("correo destino=%s asunto=%s", destino, asunto)

    @property
    def ultimo(self) -> Mensaje | None:
        return self.enviados[-1] if self.enviados else None


#: Registry of implementations, keyed by the value of ``settings.correo_proveedor``.
PROVEEDORES: dict[str, type] = {"simulado": CorreoSimulado}


def proveedor_correo(
    *,
    sesion: Session | None = None,
    notificacion: Notificacion | None = None,
) -> ProveedorCorreo:
    """Build the configured provider. Unknown names fall back to the simulation.

    The two keyword arguments are the BINDING: the RF-029 dispatcher passes the
    row it wants the outcome written to. Called with no arguments - which is
    what every INC-1B/INC-3 caller still does - it returns the unbound
    simulation.
    """
    clase = PROVEEDORES.get(settings.correo_proveedor, CorreoSimulado)
    return clase(sesion=sesion, notificacion=notificacion)


#: Default instance injected by the services, replaced by a spy in the tests.
PROVEEDOR_CORREO_PREDETERMINADO: ProveedorCorreo = proveedor_correo()
