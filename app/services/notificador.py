"""Notification port and its in-app implementation (EXTENSION POINT P8).

The MVP left this as a stub on purpose: it only wrote to the application log,
because there was no notifications table and inventing one would have meant
guessing at v0.2's schema. INC-5 brings the schema, so the stub is closed:
``NotificadorEnApp`` now persists what it delivered.

The PORT did not change. ``notificar(usuario_id, asunto, cuerpo, datos)`` is
still the whole contract, and persistence is a property of the instance - a
notifier built by :func:`notificador_en_app` with a session and a
``notificacion`` row records the delivery against it. That is why the in-app
channel can sit beside mail and push inside the same retry loop of
``notificacion_service`` without a special case.

The in-app channel is the one that never fails: the customer is not reached
over a network, the row IS the delivery, and the mobile app reads it while
polling (RF-022).
"""

import logging
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.models import Notificacion
from app.services.proveedores.registro import anotar

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

    def __init__(
        self,
        registro: logging.Logger | None = None,
        *,
        sesion: Session | None = None,
        notificacion: Notificacion | None = None,
    ) -> None:
        self._registro = registro or logger
        self._sesion = sesion
        self._notificacion = notificacion

    def notificar(
        self,
        usuario_id: int,
        asunto: str,
        cuerpo: str,
        datos: dict[str, Any] | None = None,
    ) -> None:
        anotar(self._sesion, self._notificacion)
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


def notificador_en_app(
    *,
    sesion: Session | None = None,
    notificacion: Notificacion | None = None,
) -> Notificador:
    """Build the in-app notifier, optionally bound to the row it delivers to."""
    return NotificadorEnApp(sesion=sesion, notificacion=notificacion)


#: Default instance injected by the services. Tests replace it with a spy.
NOTIFICADOR_PREDETERMINADO: Notificador = NotificadorEnApp()
