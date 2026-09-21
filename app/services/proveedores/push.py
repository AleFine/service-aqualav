"""Push port and its simulated implementation (plan section 4).

The same shape as the mail provider, one requirement heavier: the plan asks the
simulation to fail when the device token does not exist. That is not a random
coin toss - a bound provider looks the token up in ``dispositivo`` and refuses
to "deliver" to one that was never registered or was turned off. It is the
deterministic, offline equivalent of FCM answering ``UNREGISTERED``, and it is
what lets RF-029's retry path be exercised without a network.
"""

import logging
from collections import deque
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Notificacion
from app.repositories import notificacion as notificacion_repo
from app.services.proveedores.registro import anotar

logger = logging.getLogger("aqualav.push")

#: How many delivered pushes the simulation keeps for inspection.
HISTORIAL_MAXIMO = 50


@dataclass(frozen=True)
class Push:
    """One delivered push, as the simulation remembers it."""

    token_dispositivo: str
    titulo: str
    cuerpo: str


class EnvioDePushFallido(Exception):
    """Raised when the token is unknown, inactive, or the provider was told to fail."""


@runtime_checkable
class ProveedorPush(Protocol):
    """Anything able to reach a registered device."""

    def enviar(self, token_dispositivo: str, titulo: str, cuerpo: str) -> None:
        """Deliver one push, or raise :class:`EnvioDePushFallido`."""
        ...


class PushSimulado:
    """Deterministic, offline push provider. Never opens a socket."""

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
        self.enviados: deque[Push] = deque(maxlen=HISTORIAL_MAXIMO)

    def _motivo_de_rechazo(self, token_dispositivo: str) -> str | None:
        """Why this delivery cannot happen, or ``None`` when it can."""
        if self._fallar:
            return f"El proveedor de push rechazó el envío al dispositivo {token_dispositivo}."
        if not (token_dispositivo or "").strip():
            return "El envío no lleva token de dispositivo."
        if self._sesion is None:
            # Unbound: there is no device registry to check against, so the
            # simulation trusts the token, exactly like the mail one does.
            return None
        fila = notificacion_repo.obtener_dispositivo_por_token(self._sesion, token_dispositivo)
        if fila is None or not fila.activo:
            return "El dispositivo no está registrado o fue desactivado."
        return None

    def enviar(self, token_dispositivo: str, titulo: str, cuerpo: str) -> None:
        causa = self._motivo_de_rechazo(token_dispositivo)
        if causa is not None:
            anotar(self._sesion, self._notificacion, error=causa)
            raise EnvioDePushFallido(causa)

        anotar(self._sesion, self._notificacion)
        self.enviados.append(
            Push(token_dispositivo=token_dispositivo, titulo=titulo, cuerpo=cuerpo)
        )
        self._registro.info("push destino=%s titulo=%s", token_dispositivo, titulo)

    @property
    def ultimo(self) -> Push | None:
        return self.enviados[-1] if self.enviados else None


#: Registry of implementations, keyed by the value of ``settings.push_proveedor``.
PROVEEDORES: dict[str, type] = {"simulado": PushSimulado}


def proveedor_push(
    *,
    sesion: Session | None = None,
    notificacion: Notificacion | None = None,
) -> ProveedorPush:
    """Build the configured provider. Unknown names fall back to the simulation."""
    clase = PROVEEDORES.get(settings.push_proveedor, PushSimulado)
    return clase(sesion=sesion, notificacion=notificacion)


#: Default instance injected by the services, replaced by a spy in the tests.
PROVEEDOR_PUSH_PREDETERMINADO: ProveedorPush = proveedor_push()
