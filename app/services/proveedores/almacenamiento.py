"""Object storage port and its local implementation (plan section 4).

The plan gives this provider to INC-6, together with the RF-023 evidence, but
RF-027 needs it FIRST: a receipt is a PDF, and a PDF has to live somewhere that
is not a database column. So it is born here, with the exact shape section 4
asked for - ``guardar(clave, bytes, mime)`` / ``leer(clave)`` / ``url(clave)``
over a local directory - and INC-6 must reuse it rather than write a second
one. ``usuario.foto_perfil_key`` (INC-3) and ``servicio.imagen_url`` (INC-2)
are already shaped for this port.

The KEY is the contract, not the path. Callers store
``comprobantes/2026/B001-00000001.pdf`` and never a filesystem location, so
moving the bytes elsewhere is a new implementation and not a migration.

Two safety rules the local implementation keeps, because "a key is any string"
is how a student project grows a path traversal:

* a key may only contain letters, digits, ``.``, ``-``, ``_`` and ``/``;
* it may never contain ``..`` nor start with ``/``.
"""

import logging
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.config import settings

logger = logging.getLogger("aqualav.almacenamiento")

#: Everything a key is allowed to be made of.
CLAVE_VALIDA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")

#: Content type used when the caller does not say.
MIME_PREDETERMINADO = "application/octet-stream"

#: Prefix of the URL the API serves stored objects from.
RUTA_PUBLICA = "/api/v1/archivos"


class ClaveInvalida(ValueError):
    """The key is not a key: it escapes the store or carries odd characters."""


class ObjetoNoEncontrado(Exception):
    """Nothing is stored under that key."""


@runtime_checkable
class ProveedorAlmacenamiento(Protocol):
    """Anything able to keep bytes under a key and give them back."""

    def guardar(self, clave: str, contenido: bytes, mime: str = MIME_PREDETERMINADO) -> str:
        """Store ``contenido`` under ``clave`` and return the key."""
        ...

    def leer(self, clave: str) -> bytes:
        """The bytes stored under ``clave``, or :class:`ObjetoNoEncontrado`."""
        ...

    def url(self, clave: str) -> str:
        """Where the API serves that object from."""
        ...


def validar_clave(clave: str) -> str:
    """Reject anything that is not a plain, relative, printable key."""
    limpia = (clave or "").strip()
    if not limpia or ".." in limpia or limpia.startswith("/") or not CLAVE_VALIDA.match(limpia):
        raise ClaveInvalida(f"Clave de almacenamiento no válida: «{clave}».")
    return limpia


class AlmacenamientoLocal:
    """Files under a configurable directory. No network, no SDK, no bucket.

    The directory is created on demand: a store that refuses to work until
    somebody runs a setup command is a store that breaks "clone and run"
    (RNF-015).
    """

    def __init__(
        self, directorio: Path | str | None = None, registro: logging.Logger | None = None
    ) -> None:
        self._raiz = (
            Path(directorio) if directorio is not None else settings.directorio_almacenamiento
        )
        self._registro = registro or logger

    @property
    def raiz(self) -> Path:
        return self._raiz

    def _ruta(self, clave: str) -> Path:
        return self._raiz / validar_clave(clave)

    def guardar(self, clave: str, contenido: bytes, mime: str = MIME_PREDETERMINADO) -> str:
        ruta = self._ruta(clave)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(contenido)
        self._registro.info(
            "almacenamiento guardado clave=%s bytes=%s mime=%s", clave, len(contenido), mime
        )
        return validar_clave(clave)

    def leer(self, clave: str) -> bytes:
        ruta = self._ruta(clave)
        if not ruta.is_file():
            raise ObjetoNoEncontrado(f"No hay ningún objeto almacenado en «{clave}».")
        return ruta.read_bytes()

    def url(self, clave: str) -> str:
        return f"{RUTA_PUBLICA}/{validar_clave(clave)}"


#: Registry of implementations, keyed by ``settings.almacenamiento_proveedor``.
PROVEEDORES: dict[str, type] = {"simulado": AlmacenamientoLocal}


def proveedor_almacenamiento(*, directorio: Path | str | None = None) -> ProveedorAlmacenamiento:
    """Build the configured store. Unknown names fall back to the local one."""
    clase = PROVEEDORES.get(settings.almacenamiento_proveedor, AlmacenamientoLocal)
    return clase(directorio=directorio)
