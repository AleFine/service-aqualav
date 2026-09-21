"""Simulated external providers (contract section 4 of the v1.0 plan).

Every external service the SRS names is reached through a PORT (a ``Protocol``)
whose default implementation is local, deterministic and offline. Selecting one
is a setting with a working default, so the API boots without touching ``.env``.

INC-1B opened the package with the mail port (RF-035: the temporary password).
INC-5 adds the push one and teaches both to persist what they delivered in
``notificacion`` (RF-029). Payments, object storage and PDF land in their own
increments and belong in this package as well.
"""

from app.services.proveedores.correo import (
    PROVEEDOR_CORREO_PREDETERMINADO,
    CorreoSimulado,
    EnvioDeCorreoFallido,
    Mensaje,
    ProveedorCorreo,
    proveedor_correo,
)
from app.services.proveedores.push import (
    PROVEEDOR_PUSH_PREDETERMINADO,
    EnvioDePushFallido,
    ProveedorPush,
    Push,
    PushSimulado,
    proveedor_push,
)
from app.services.proveedores.registro import anotar

__all__ = [
    "PROVEEDOR_CORREO_PREDETERMINADO",
    "PROVEEDOR_PUSH_PREDETERMINADO",
    "CorreoSimulado",
    "EnvioDeCorreoFallido",
    "EnvioDePushFallido",
    "Mensaje",
    "ProveedorCorreo",
    "ProveedorPush",
    "Push",
    "PushSimulado",
    "anotar",
    "proveedor_correo",
    "proveedor_push",
]
