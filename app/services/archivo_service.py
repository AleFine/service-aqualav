"""The generic object door of the simulated store (plan section 4).

INC-4 built ``ProveedorAlmacenamiento`` / ``AlmacenamientoLocal`` because the
receipt of RF-027 needed somewhere to live, and left exactly one way in:
``GET /comprobantes/{id}/archivo``. Three columns were already shaped for that
store and had no door at all - ``usuario.foto_perfil_key`` (RF-006, INC-3),
``servicio.imagen_url`` (RF-009, INC-2) and, from this increment,
``evidencia.objeto_key`` (RF-023). This module is that door, and it is
deliberately thin: the PORT is not re-implemented here, it is reused.

Three rules it adds on top of the provider, because "any string is a key" is
how a student project grows a hole:

1. **the caller never composes a key.** It picks a FOLDER - what the object is
   - and the name is an opaque random token. Keys are therefore not guessable
   and cannot collide, which is what makes serving them by URL reasonable;
2. **only pictures get in**, by declared media type, with the extension derived
   from it rather than from whatever the client called the file;
3. **size is capped** at ``settings.archivo_tamano_maximo_kb`` (RNF-004 M4:
   one megabyte). RF-023 flow 3a is the backend half of that rule: the file is
   REFUSED with its reason, and compressing it again or picking another picture
   is the mobile app's call.

The bytes arrive base64 encoded inside a JSON body - see
``app/schemas/archivo.py`` for why that is not an oversight.
"""

import base64
import binascii
import secrets

from app.config import settings
from app.core.errors import ArchivoRechazado, RecursoNoEncontrado, detalle
from app.models import CarpetaArchivo
from app.services.proveedores.almacenamiento import (
    ClaveInvalida,
    ObjetoNoEncontrado,
    ProveedorAlmacenamiento,
    proveedor_almacenamiento,
)

#: Media types the store accepts, mapped to the extension the key carries.
#: An allowlist and not a denylist: RF-023 is about PHOTOGRAPHS, and an object
#: store that will take anything is a file upload vulnerability with a nicer
#: name (RNF-013).
TIPOS_PERMITIDOS: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
}

#: The reverse map, used to answer ``GET /archivos/...`` with a honest
#: ``Content-Type``. The store keeps bytes, not metadata, so the extension of
#: the key is what says what they are - which is why this module is the only
#: place allowed to build one.
TIPOS_POR_EXTENSION: dict[str, str] = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
    "pdf": "application/pdf",
}

#: Fallback when the extension says nothing recognisable.
MIME_PREDETERMINADO = "application/octet-stream"

#: Length in bytes of the random part of a key. Sixteen bytes of ``secrets``
#: is not a password, but it is far past guessable, which is all a capability
#: style URL needs.
LONGITUD_TOKEN = 16


def limite_bytes() -> int:
    """The maximum size one object may have, in bytes (RNF-004 M4)."""
    return max(1, settings.archivo_tamano_maximo_kb) * 1024


def normalizar_mime(mime: str | None) -> str:
    """The media type, lowercased, or a refusal naming what IS accepted."""
    limpio = (mime or "").split(";")[0].strip().lower()
    if limpio not in TIPOS_PERMITIDOS:
        aceptados = ", ".join(sorted({tipo for tipo in TIPOS_PERMITIDOS}))
        raise ArchivoRechazado(
            "Ese tipo de archivo no se admite. Envía una fotografía.",
            detalles=[detalle("mime", f"Tipos aceptados: {aceptados}.")],
        )
    return limpio


def decodificar(contenido_base64: str) -> bytes:
    """The bytes behind the base64 text, or a refusal that says which it was."""
    try:
        contenido = base64.b64decode((contenido_base64 or "").encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as error:
        raise ArchivoRechazado(
            "El contenido del archivo no es base64 válido. Vuelve a enviarlo.",
            detalles=[detalle("contenido_base64", str(error)[:200])],
        ) from error
    if not contenido:
        raise ArchivoRechazado(
            "El archivo llegó vacío. Vuelve a tomar la fotografía e inténtalo otra vez.",
            detalles=[detalle("contenido_base64", "No contiene ningún byte.")],
        )
    return contenido


def validar_tamano(contenido: bytes) -> bytes:
    """RF-023 flow 3a: over the limit, refused WITH ITS REASON."""
    maximo = limite_bytes()
    if len(contenido) > maximo:
        raise ArchivoRechazado(
            "La imagen supera el tamaño máximo permitido. " "Comprímela o elige otra fotografía.",
            detalles=[
                detalle("contenido_base64", f"Pesa {len(contenido)} bytes."),
                detalle("limite_bytes", str(maximo)),
            ],
        )
    return contenido


def clave_nueva(carpeta: CarpetaArchivo | str, mime: str, *, prefijo: str | None = None) -> str:
    """An opaque key inside ``carpeta``: ``perfiles/<token>.jpg``.

    ``prefijo`` lets a caller group objects that belong together - the evidence
    of one service goes under ``evidencias/<reserva_id>/`` - without ever
    choosing the name, which stays random.
    """
    valor = getattr(carpeta, "value", carpeta)
    extension = TIPOS_PERMITIDOS[mime]
    partes = [str(valor)]
    if prefijo:
        partes.append(str(prefijo))
    partes.append(f"{secrets.token_hex(LONGITUD_TOKEN)}.{extension}")
    return "/".join(partes)


def mime_de_clave(clave: str) -> str:
    """What a stored object is, read from the extension of its key."""
    _, _, extension = clave.rpartition(".")
    return TIPOS_POR_EXTENSION.get(extension.lower(), MIME_PREDETERMINADO)


def url(clave: str, *, almacenamiento: ProveedorAlmacenamiento | None = None) -> str:
    """Where the API serves that object from."""
    almacenamiento = almacenamiento or proveedor_almacenamiento()
    return almacenamiento.url(clave)


def guardar(
    contenido: bytes,
    mime: str,
    carpeta: CarpetaArchivo | str,
    *,
    prefijo: str | None = None,
    almacenamiento: ProveedorAlmacenamiento | None = None,
) -> str:
    """Store already decoded and already validated bytes, returning the key."""
    almacenamiento = almacenamiento or proveedor_almacenamiento()
    clave = clave_nueva(carpeta, mime, prefijo=prefijo)
    return almacenamiento.guardar(clave, contenido, mime)


def subir(
    contenido_base64: str,
    mime: str,
    carpeta: CarpetaArchivo | str,
    *,
    prefijo: str | None = None,
    almacenamiento: ProveedorAlmacenamiento | None = None,
) -> tuple[str, bytes, str]:
    """Decode, validate and store one object: ``(clave, contenido, mime)``.

    The whole pipeline in one call, so the two doors that upload - the generic
    endpoint and the evidence of RF-023 - cannot drift apart on what they
    accept.
    """
    tipo = normalizar_mime(mime)
    contenido = validar_tamano(decodificar(contenido_base64))
    clave = guardar(contenido, tipo, carpeta, prefijo=prefijo, almacenamiento=almacenamiento)
    return clave, contenido, tipo


def leer(clave: str, *, almacenamiento: ProveedorAlmacenamiento | None = None) -> bytes:
    """The stored bytes, or 404.

    A key that escapes the store and a key nothing was ever written under get
    the SAME answer: a probing caller must not be able to tell them apart.
    """
    almacenamiento = almacenamiento or proveedor_almacenamiento()
    try:
        return almacenamiento.leer(clave)
    except (ObjetoNoEncontrado, ClaveInvalida) as error:
        raise RecursoNoEncontrado(
            "No encontramos ese archivo. Vuelve a subirlo o pide el enlace otra vez.",
            detalles=[detalle("clave", str(error)[:200])],
        ) from error
