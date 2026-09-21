"""What a simulated provider writes down about a delivery (RF-029).

Shared by the mail and the push simulations so both leave the same trail. It
exists because the plan's section 4 asks each provider to "persist the message
in the ``notificacion`` table and log it": the port stays
``enviar(destino, asunto, cuerpo)``, and persistence is a property of the
INSTANCE - a provider built with a session and a row updates it, one built
without them behaves exactly like the INC-1B simulation did.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.horario import ahora_utc
from app.models import EstadoEnvio, Notificacion
from app.repositories import notificacion as notificacion_repo


def anotar(
    sesion: Session | None,
    fila: Notificacion | None,
    *,
    error: str | None = None,
    momento: datetime | None = None,
) -> None:
    """Record one attempt against the bound row, if there is one.

    A provider with no binding persists nothing and raises nothing: a
    notification is never allowed to break the flow that produced it, and the
    accounts mail of RF-001/RF-003/RF-035 keeps its INC-1B behaviour untouched.
    """
    if sesion is None or fila is None:
        return
    notificacion_repo.anotar_intento(
        sesion,
        fila,
        estado_envio=(EstadoEnvio.FALLIDA if error else EstadoEnvio.ENVIADA).value,
        momento=momento or ahora_utc(),
        error=error,
    )
