"""Simulated external providers (contract section 4 of the v1.0 plan).

Every external service the SRS names is reached through a PORT (a ``Protocol``)
whose default implementation is local, deterministic and offline. Selecting one
is a setting with a working default, so the API boots without touching ``.env``.

INC-1B opened the package with the mail port (RF-035: the temporary password).
INC-5 added the push one and taught both to persist what they delivered in
``notificacion`` (RF-029). INC-4 adds the three the money needs:

* ``ProveedorPasarela`` / ``PasarelaSimulada`` - deterministic **by test card
  number**, with a ledger in ``transaccion_pasarela`` so ``consultar`` answers
  the same thing for the same idempotency key (RF-026 flow 3b);
* ``ProveedorAlmacenamiento`` / ``AlmacenamientoLocal`` - files under a
  configurable directory. The plan gave this one to INC-6 with the RF-023
  evidence, but RF-027 needs it first, so it is born here and **INC-6 must
  reuse it** rather than write a second one;
* ``ProveedorDocumentos`` / ``DocumentosSimulados`` - a real, minimal PDF 1.4
  written by hand, with no new dependency.
"""

from app.services.proveedores.almacenamiento import (
    AlmacenamientoLocal,
    ClaveInvalida,
    ObjetoNoEncontrado,
    ProveedorAlmacenamiento,
    proveedor_almacenamiento,
)
from app.services.proveedores.correo import (
    PROVEEDOR_CORREO_PREDETERMINADO,
    CorreoSimulado,
    EnvioDeCorreoFallido,
    Mensaje,
    ProveedorCorreo,
    proveedor_correo,
)
from app.services.proveedores.documentos import (
    MIME_PDF,
    DocumentosSimulados,
    ProveedorDocumentos,
    proveedor_documentos,
)
from app.services.proveedores.pasarela import (
    TARJETAS_DE_PRUEBA,
    PasarelaNoDisponible,
    PasarelaSimulada,
    ProveedorPasarela,
    ResultadoPasarela,
    TiempoDeEsperaAgotado,
    proveedor_pasarela,
    tokenizar,
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
    "MIME_PDF",
    "PROVEEDOR_CORREO_PREDETERMINADO",
    "PROVEEDOR_PUSH_PREDETERMINADO",
    "TARJETAS_DE_PRUEBA",
    "AlmacenamientoLocal",
    "ClaveInvalida",
    "CorreoSimulado",
    "DocumentosSimulados",
    "EnvioDeCorreoFallido",
    "EnvioDePushFallido",
    "Mensaje",
    "ObjetoNoEncontrado",
    "PasarelaNoDisponible",
    "PasarelaSimulada",
    "ProveedorAlmacenamiento",
    "ProveedorCorreo",
    "ProveedorDocumentos",
    "ProveedorPasarela",
    "ProveedorPush",
    "Push",
    "PushSimulado",
    "ResultadoPasarela",
    "TiempoDeEsperaAgotado",
    "anotar",
    "proveedor_almacenamiento",
    "proveedor_correo",
    "proveedor_documentos",
    "proveedor_pasarela",
    "proveedor_push",
    "tokenizar",
]
