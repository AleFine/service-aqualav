"""Simulated external providers (contract section 4 of the v1.0 plan).

Every external service the SRS names is reached through a PORT (a ``Protocol``)
whose default implementation is local, deterministic and offline. Selecting one
is a setting with a working default, so the API boots without touching ``.env``.

INC-1B only needs the mail port (RF-035: the temporary password). Payments,
object storage, PDF and the scheduler land in their own increments and belong
in this package as well.
"""

from app.services.proveedores.correo import (
    PROVEEDOR_CORREO_PREDETERMINADO,
    CorreoSimulado,
    Mensaje,
    ProveedorCorreo,
    proveedor_correo,
)

__all__ = [
    "PROVEEDOR_CORREO_PREDETERMINADO",
    "CorreoSimulado",
    "Mensaje",
    "ProveedorCorreo",
    "proveedor_correo",
]
