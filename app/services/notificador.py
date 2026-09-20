"""Notification port and its single MVP implementation (EXTENSION POINT P8).

RF-022 is served by polling in the MVP, so there is nothing to push yet. The
port exists anyway because the moment RF-029 lands in v0.2, adding push is
registering a second implementation of :class:`Notificador` - the reservation
and operation services keep calling ``notificar`` exactly where they do today.

The MVP implementation only writes to the application log: there is no
notifications table, and building one now would be guessing at v0.2's schema.
"""

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("aqualav.notificador")


@runtime_checkable
class Notificador(Protocol):
    """Anything able to reach a user with a message."""

    def notificar(
        self,
        usuario_id: int,
        asunto: str,
        cuerpo: str,
        datos: dict[str, Any] | None = None,
    ) -> None:
        """Deliver one notice. Implementations must never raise on delivery."""
        ...


class NotificadorEnApp:
    """In-app notices: the user sees them next time the app polls (RF-022).

    Delivery failures are swallowed on purpose. A notification is never allowed
    to roll back the business transaction that produced it.
    """

    def __init__(self, registro: logging.Logger | None = None) -> None:
        self._registro = registro or logger

    def notificar(
        self,
        usuario_id: int,
        asunto: str,
        cuerpo: str,
        datos: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._registro.info(
                "notificacion usuario=%s asunto=%s cuerpo=%s datos=%s",
                usuario_id,
                asunto,
                cuerpo,
                datos or {},
            )
        except Exception:  # pragma: no cover - defensive, logging must not break a flow
            pass


#: Default instance injected by the services. Tests replace it with a spy and
#: v0.2 replaces it with a composite that also pushes.
NOTIFICADOR_PREDETERMINADO: Notificador = NotificadorEnApp()
